import frappe
from frappe.tests.utils import FrappeTestCase

from toolbox.utils import IndexCandidate, Query, QueryBenchmark, Table, record_query, record_table


class TestToolBoxUtils(FrappeTestCase):
    def tearDown(self) -> None:
        frappe.db.rollback()
        return super().tearDown()

    def test_record_table(self):
        table_name = "tabTestTable"

        table_id = record_table(table_name)
        self.assertIsNotNone(table_id)
        self.assertNotEqual(table_id, table_name)
        self.assertTrue(frappe.db.exists("MariaDB Table", table_id))

    def test_record_query(self):
        query = "SELECT * FROM `tabTestTable`"

        mqry = record_query(query).save()
        self.assertEqual(mqry.occurence, 1)
        mqry_again = record_query(query).save()
        self.assertEqual(mqry.name, mqry_again.name)
        self.assertEqual(mqry_again.occurence, 2)

    def test_table_find_index_where_candidates(self):
        queries = [
            Query(
                "select `name` from `tabNote` where `modified` = `creation` or `creation` > `modified`"
            )
        ]
        table_id = frappe.db.get_value("MariaDB Table", {"_table_name": "tabNote"})
        table = Table(table_id)
        index_candidates = table.find_index_candidates(queries)
        self.assertEqual(index_candidates, [["modified", "creation"], ["creation", "modified"]])

        queries = [
            Query(
                "select `name` from `tabNote` where `modified` = `creation` or `creation` > '2023-02-13 13:35:01.556111' order by `title`"
            ),
        ]
        index_candidates = table.find_index_candidates(queries)
        self.assertEqual(index_candidates, [["modified", "creation"], ["creation"]])

    def test_table_find_index_select_candidates(self):
        table_id = frappe.db.get_value("MariaDB Table", {"_table_name": "tabQuality Goal"})
        table = Table(table_id)

        q_1 = Query(
            "select `name`, `frequency`, `date`, `weekday` from `tabQuality Goal` order by `tabQuality Goal`.`modified` DESC",
            table=table,
        )
        ic_1 = IndexCandidate(query=q_1)
        ic_1_columnset = ["name", "frequency", "date", "weekday", "modified"]
        ic_1.extend(ic_1_columnset)

        q_2 = Query(
            "select `name` as `aliased_name` from `tabQuality Goal` order by `tabQuality Goal`.`modified` DESC",
            table=table,
        )
        ic_2 = IndexCandidate(query=q_2)
        ic_2_columnset = ["name", "modified"]
        ic_2.extend(ic_2_columnset)

        index_candidates = table.find_index_candidates([q_1, q_2])

        self.assertEqual(index_candidates[0], ic_1)
        self.assertEqual(index_candidates[0], ic_1_columnset)
        self.assertEqual(index_candidates[1], ic_2)
        self.assertEqual(index_candidates[1], ic_2_columnset)

    def test_query_benchmark_no_changes(self):
        index_candidates = [
            IndexCandidate(query=Query(qry))
            for qry in (
                "SELECT 1",
                "SELECT `name` from `tabNote`",
            )
        ]

        with QueryBenchmark(index_candidates=index_candidates) as qbm:
            ...
        results = dict(qbm.get_unchanged_results())

        self.assertEqual(len(results), 2)
        for _, data in results.items():
            self.assertDictEqual(data["before"], data["after"])
