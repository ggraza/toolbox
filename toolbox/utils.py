import re
from contextlib import contextmanager
from enum import Enum, auto
from functools import lru_cache
from typing import TYPE_CHECKING, Callable

from click import secho
from sql_metadata import Parser, QueryType
from sqlparse import format as format_sql
from sqlparse import parse
from sqlparse.sql import Comparison, Identifier, Where
from sqlparse.tokens import Keyword

if TYPE_CHECKING:
    from sqlparse.sql import Statement

    from toolbox.doctypes import MariaDBQuery

PARAMS_PATTERN = re.compile(r"\%\([\w]*\)s")


def record_table(table: str) -> str:
    from html import escape

    import frappe

    table = table or "NULL"

    if table_id := frappe.get_all("MariaDB Table", {"_table_name": table}, limit=1, pluck="name"):
        table_id = table_id[0]
    # handle derived tables & such
    elif table_id := frappe.get_all(
        "MariaDB Table",
        {"_table_name": escape(table)},
        limit=1,
        pluck="name",
    ):
        table_id = table_id[0]
    # generate temporary table names
    else:
        table_record = frappe.new_doc("MariaDB Table")
        table_record._table_name = table
        table_record.insert()
        table_id = table_record.name

    return table_id


def record_query(
    query: str, p_query: str | None = None, call_stack: list[dict] | None = None
) -> "MariaDBQuery":
    import frappe

    if query_name := frappe.get_all("MariaDB Query", {"query": query}, limit=1):
        query_record = frappe.get_doc("MariaDB Query", query_name[0])
        query_record.parameterized_query = p_query
        query_record.occurence += 1

        if call_stack:
            # TODO: Let's just maintain one stack for now
            # if not query_record.call_stack:
            query_record.call_stack = frappe.as_json(call_stack)

        return query_record

    query_record = frappe.new_doc("MariaDB Query")
    query_record.query = query
    query_record.parameterized_query = p_query
    query_record.occurence = 1
    query_record.call_stack = frappe.as_json(call_stack)

    return query_record


def record_database_state():
    import frappe

    for tbl in frappe.db.get_tables(cached=False):
        if not frappe.db.exists("MariaDB Table", {"_table_name": tbl}):
            table_record = frappe.new_doc("MariaDB Table")
            table_record._table_name = tbl
            table_record._table_exists = True
            table_record.db_insert()


@contextmanager
def check_dbms_compatibility(conf):
    if conf.db_type != "mariadb":
        secho(f"WARN: This command might not be compatible with {conf.db_type}", fg="yellow")
    yield


@contextmanager
def handle_redis_connection_error():
    from redis.exceptions import ConnectionError

    try:
        yield
    except ConnectionError as e:
        secho(f"ERROR: {e}", fg="red")
        secho("NOTE: Make sure Redis services are running", fg="yellow")


def process_sql_metadata_chunk(
    queries: list[dict],
    site: str,
    setup: bool = True,
    chunk_size: int = 5_000,
    auto_commit: bool = True,
):
    import frappe
    from sqlparse import format as sql_format

    with frappe.init_site(site):
        sql_count = 0
        granularity = chunk_size // 100
        frappe.connect()

        TOOLBOX_TABLES = set(frappe.get_all("DocType", {"module": "Toolbox"}, pluck="name"))

        if setup:
            record_database_state()

        for query_info in queries:
            query: str = query_info["query"]

            if not query.lower().startswith(("select", "insert", "update", "delete")):
                continue

            parameterized_query: str = query_info["args"][0]

            # should check warnings too? unsure at this point
            explain_data = frappe.db.sql(f"EXPLAIN EXTENDED {query}", as_dict=True)

            if not explain_data:
                print(f"Cannot explain query: {query}")
                continue

            # Note: Desk doesn't like Queries with whitespaces in long text for show title in links for forms
            # Better to strip them off and format on demand
            query_record = record_query(
                sql_format(query, strip_whitespace=True, keyword_case="upper"),
                p_query=parameterized_query,
                call_stack=query_info["stack"],
            )
            for explain in explain_data:
                # skip Toolbox internal queries
                if explain["table"] not in TOOLBOX_TABLES:
                    query_record.apply_explain(explain)
            query_record.save()

            sql_count += 1
            # Show approximate progress
            print(
                f"Processed ~{round(sql_count / granularity) * granularity:,} queries per job"
                + " " * 5,
                end="\r",
            )

            if auto_commit and frappe.db.transaction_writes > chunk_size:
                frappe.db.commit()

        if auto_commit:
            frappe.db.commit()


@lru_cache(maxsize=None)
def get_table_name(table_id: str):
    # Note: Use this util only via CLI / single threaded
    import frappe

    return frappe.db.get_value("MariaDB Table", table_id, "_table_name")


@lru_cache(maxsize=None)
def get_table_id(table_name: str):
    # Note: Use this util only via CLI / single threaded
    import frappe

    return frappe.db.get_value("MariaDB Table", {"_table_name": table_name}, "name")


class Query:
    def __init__(self, sql: str, occurence: int = 1, table: "Table" = None) -> None:
        self.sql = sql.strip()
        self.occurence = occurence
        self.table = table

    def __repr__(self) -> str:
        sub = f", table={self.table}" if self.table else ""
        dotted = "..." if len(self.sql) > 11 else ""
        return f"Query({self.sql[:10]}{dotted}{sub})"

    # Note: We're essentially parsing the same query multiple times
    # TODO: Avoid this, pass the parsed query to sql-metadata instead (or similar)
    @property
    def parsed(self) -> "Statement":
        if not hasattr(self, "_parsed"):
            self._parsed = parse(self.sql)[0]
        return self._parsed

    @property
    def d_parsed(self):
        if not hasattr(self, "_d_parsed"):
            self._d_parsed = Parser(self.sql)
        return self._d_parsed

    def get_sample(self) -> str:
        ret = self.sql

        if "%s" in self.sql:
            ret = ret.replace("%s", "1")

        else:
            for k, v in ((p, "1") for p in PARAMS_PATTERN.findall(self.sql)):
                ret = ret.replace(k, v)

        return format_sql(ret, strip_whitespace=True, keyword_case="upper")


class IndexCandidateType(Enum):
    SELECT: str = auto()
    WHERE: str = auto()


class IndexCandidate(list):
    def __init__(self, query: Query, type: IndexCandidateType | None = None) -> None:
        self.query = query
        self.type = type or IndexCandidateType.WHERE

    def __repr__(self) -> str:
        return f"IndexCandidate({self.query.table or 'unspecified'}, {super().__repr__()})"

    def append(self, __object: str) -> None:
        if __object in self:
            return
        return super().append(__object)


class Table:
    def __init__(self, id: str) -> None:
        self.id = id
        self.name = get_table_name(self.id)

    def __repr__(self) -> str:
        return f"Table({self.name}, name={self.id})"

    def __str__(self) -> str:
        return self.name

    def exists(self) -> bool:
        import frappe

        return bool(frappe.db.sql("SHOW TABLES LIKE %s", self.name))

    def find_index_candidates(
        self, queries: list[Query], qualifier: Callable | None = None
    ) -> list[IndexCandidate]:
        index_candidates = []

        for query in queries:
            if qualifier and not qualifier(query):
                continue

            # TODO: handle subqueries by making this recursive
            if any(isinstance(token, Where) for token in query.parsed):
                for c in self.find_index_candidates_from_where_query(query):
                    if c and c not in index_candidates:
                        index_candidates.append(c)
            elif ic := self.find_index_candidates_from_select_query(query):
                index_candidates.append(ic)

        return index_candidates

    def find_index_candidates_from_where_query(self, query: Query) -> list[IndexCandidate]:
        query_index_candidate = []
        ic_operator = "AND"
        # IndexCandidate generation ruleset:
        # where A | B | C = [ic(A), ic(B), ic(C)]
        # where A & B & C = [ic(A, B, C)]
        # where A & B | C = [ic(A, B), ic(C)]

        for clause_token in query.parsed.tokens:
            # check only the where clause
            if not isinstance(clause_token, Where):
                continue

            # we may want to check type of operators for finding appropriate index types at this stage
            for in_token in clause_token.tokens:
                if in_token.ttype == Keyword and in_token.value.upper() in {"AND", "OR"}:
                    ic_operator = in_token.value.upper()

                if not isinstance(in_token, Comparison):
                    continue

                if ic_operator == "OR":
                    index_candidate = IndexCandidate(query=query, type=IndexCandidateType.WHERE)
                else:
                    index_candidate = (
                        query_index_candidate[-1]
                        if query_index_candidate
                        else IndexCandidate(query=query, type=IndexCandidateType.WHERE)
                    )

                for inner_token in in_token.tokens:
                    if not isinstance(inner_token, Identifier):
                        continue
                    if inner_token.get_parent_name() in {None, self.name}:
                        index_candidate.append(inner_token.get_name())

                if index_candidate not in query_index_candidate:
                    query_index_candidate.append(index_candidate)

        return query_index_candidate

    def find_index_candidates_from_select_query(self, query: Query) -> IndexCandidate:
        query_index_candidate = IndexCandidate(query=query, type=IndexCandidateType.SELECT)
        if query.d_parsed.query_type != QueryType.SELECT:
            return query_index_candidate

        for column in query.d_parsed.columns:
            if "." in column:
                tbl, col = column.split(".")

                if not query.table:
                    query_index_candidate.append(col)
                elif tbl == query.table.name:
                    query_index_candidate.append(col)
            else:
                query_index_candidate.append(column)

        return query_index_candidate

    def qualify_index_candidates(self, index_candidates: list[IndexCandidate]):
        from toolbox.doctypes import MariaDBIndex

        # TODO: Add something to resolve / reduce to a better index -
        # like if there are multiple columns in the query, create a composite index
        # * then covering index, etc etc

        current_indexes = MariaDBIndex.get_indexes(self.name, reduce=True)
        required_indexes = [x for x in index_candidates if x not in current_indexes]

        return required_indexes


class QueryBenchmark:
    def __init__(self, index_candidates: list[IndexCandidate], verbose=False):
        self.index_candidates = index_candidates
        self.verbose = verbose
        self.before = []
        self.after = []

    def __enter__(self):
        self.before = self.conduct_benchmark()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.after = self.conduct_benchmark()

    def conduct_benchmark(self) -> list[list[dict]]:
        import frappe

        return [
            frappe.db.sql(f"ANALYZE {ic.query.get_sample()}", as_dict=True, debug=self.verbose)
            for ic in self.index_candidates
        ]

    def compare_results(
        self, before: list[list[dict]], after: list[list[dict]]
    ) -> list[list[dict]]:
        from collections import defaultdict

        from frappe.utils import flt

        results = [
            [{"before": defaultdict(dict), "after": defaultdict(dict)}] * len(before)
        ] * len(before)

        for i, (before_data, after_data) in enumerate(zip(before, after)):
            for j, (before_row, after_row) in enumerate(zip(before_data, after_data)):
                for key in {"r_rows", "r_filtered", "Extra"}:
                    results[i][j]["after"][key] = flt(after_row[key])
                    results[i][j]["before"][key] = flt(before_row[key])

        if self.verbose:
            print(results)

        return results

    def get_unchanged_results(self):
        for q_id, context_table in enumerate(self.compare_results(self.before, self.after)):
            changes_detected = False

            for row_id, context in enumerate(context_table):
                # if the number of rows read is the same, then the index is not helping
                rows_read_changed = context["before"]["r_rows"] != context["after"]["r_rows"]

                # r_filtered relates to how many rows were read and filtered out,
                # higher the value, better the index - r_filtered = 100 best
                rows_selectivity_changed = (
                    context["before"]["r_filtered"] != context["after"]["r_filtered"]
                )

                # if the number of rows read and the selectivity of the index has not changed, then the index is not helping
                if not rows_read_changed and not rows_selectivity_changed:
                    ...
                # if the selectivity has gotten worse, then the index is not helping
                elif (
                    rows_selectivity_changed
                    and context["before"]["r_filtered"] > context["after"]["r_filtered"]
                ):
                    ...
                else:
                    changes_detected = True

            if not changes_detected:
                yield q_id, context
