import tempfile
import unittest
from pathlib import Path
from xml.etree import ElementTree as ET

from catalog.index import KNOWLEDGE, build, search


class CatalogSearchTest(unittest.TestCase):
    def test_mdaemon_family_capacity_and_purchase_type(self):
        with tempfile.TemporaryDirectory() as directory:
            feed = Path(directory) / "test.xml"
            index = Path(directory) / "test.sqlite3"
            root = ET.Element("yml_catalog", date="test")
            offers = ET.SubElement(ET.SubElement(root, "shop"), "offers")
            for key, product, seats, kind in (
                ("a", "MDaemon Messaging Server", 5, "Полная версия"),
                ("b", "MDaemon Messaging Server", 20, "Полная версия"),
                ("c", "MDaemon Messaging Server", 20, "Продление"),
                ("d", "MDaemon Messaging Server", 30, "Полная версия"),
                ("e", "MDaemon Connector for Outlook", 20, "Полная версия"),
                ("f", "Example Standard", 20, "Полная версия"),
            ):
                row = ET.SubElement(offers, "offer", id=key)
                ET.SubElement(row, "name").text = product
                ET.SubElement(row, "param", name="К-во пользователей до").text = str(seats)
                ET.SubElement(row, "param", name="Тип лицензии").text = kind
            ET.ElementTree(root).write(feed, encoding="utf-8")
            build(feed, index, KNOWLEDGE)

            def ids(query, **options):
                return [r["id"] for r in search(index, query, **options)]

            self.assertEqual(set(ids("MDaemon Standard")), {"a", "b", "c", "d"})
            self.assertEqual(set(ids("Нужен Мдеймон на 20 человек", limit=2)), {"b", "c"})
            self.assertEqual(ids("MDaemon Standard на 20 мест", license_type="Продление"), ["c"])
            self.assertEqual(ids("MDaemon Standard на 25 мест"), ["d"])
            self.assertEqual(ids("MDaemon Standard на 40 мест"), [])
            self.assertEqual(ids("MDaemon Connector for Outlook на 20 мест"), ["e"])
            self.assertEqual(ids("Example Standard", limit=1), ["f"])
