import unittest

from linguoai.cache import LRUCache


class CacheTests(unittest.TestCase):
    def test_get_refreshes_lru_order(self) -> None:
        cache: LRUCache[str, str] = LRUCache(max_size=2)
        cache.set("a", "A")
        cache.set("b", "B")
        self.assertEqual(cache.get("a"), "A")
        cache.set("c", "C")
        self.assertIsNone(cache.get("b"))
        self.assertEqual(cache.get("a"), "A")
        self.assertEqual(cache.get("c"), "C")


if __name__ == "__main__":
    unittest.main()
