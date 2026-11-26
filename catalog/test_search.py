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
                ("g", "MDaemon Technologies Security Gateway for Email Servers", 50, "Полная версия"),
                ("h", "MDaemon Messaging Server", 50, "Продление"),
            ):
                row = ET.SubElement(offers, "offer", id=key)
                ET.SubElement(row, "name").text = product
                ET.SubElement(row, "param", name="К-во пользователей до").text = str(seats)
                ET.SubElement(row, "param", name="Тип лицензии").text = kind
                if key == "g":
                    ET.SubElement(row, "param", name="Срок действия").text = "Не определено"
                    for platform in ("Windows", "Linux"):
                        ET.SubElement(row, "param", name="Платформа").text = platform
            ET.ElementTree(root).write(feed, encoding="utf-8")
            build(feed, index, KNOWLEDGE)

            def ids(query, **options):
                return [r["id"] for r in search(index, query, **options)]

            self.assertEqual(set(ids("MDaemon Standard")), {"a", "b", "c", "d", "h"})
            self.assertEqual(set(ids("Нужен Мдеймон на 20 человек", limit=2)), {"b", "c"})
            self.assertEqual(ids("MDaemon Standard на 20 мест", license_type="Продление"), ["c", "h"])
            self.assertEqual(ids("MDaemon Standard на 25 мест"), ["d", "h"])
            self.assertEqual(ids("MDaemon Standard на 60 мест"), [])
            self.assertEqual(ids("MDaemon Connector for Outlook на 20 мест"), ["e"])
            self.assertEqual(ids("Example Standard", limit=1), ["f"])
            for spelling in ("SecurityGateway", "Security Gateway"):
                self.assertEqual(ids(f"MDaemon {spelling} на 50 пользователей"), ["g"])
            gateway = search(index, "MDaemon SecurityGateway")[0]
            server = search(index, "MDaemon Messaging Server на 50 мест")[0]
            self.assertEqual(gateway["details"]["raw_params"]["Срок действия"], ["Не определено"])
            self.assertNotIn("Срок действия", server["details"]["raw_params"])
            self.assertNotIn("Срок действия", gateway["params"])
            self.assertNotIn("Срок действия", server["params"])
            self.assertEqual(gateway["details"]["raw_params"]["Платформа"], ["Windows", "Linux"])
