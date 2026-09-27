import unittest

from mydb import DatabaseError
from tests.helpers import TempDBTest


class TestSQL(TempDBTest):
    def setUp(self):
        super().setUp()
        self.db.execute_script("""
            CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT NOT NULL,
                                email TEXT UNIQUE, age INTEGER, city TEXT);
            INSERT INTO users (name, email, age, city) VALUES
                ('Adi', 'adi@x.com', 24, 'Cardiff'),
                ('Bob', 'bob@x.com', 30, 'London'),
                ('Cara', NULL, 19, 'Cardiff'),
                ('Dan', 'dan@x.com', NULL, 'London');
        """)

    def test_select_star(self):
        r = self.db.execute("SELECT * FROM users")
        self.assertEqual(r.columns, ["id", "name", "email", "age", "city"])
        self.assertEqual(len(r.rows), 4)
        self.assertEqual(r.rows[0], (1, "Adi", "adi@x.com", 24, "Cardiff"))

    def test_where_order_limit(self):
        self.assertEqual(self.rows("SELECT name FROM users WHERE age > 18 ORDER BY age DESC LIMIT 2"),
                         [("Bob",), ("Adi",)])
        self.assertEqual(self.rows("SELECT name FROM users ORDER BY name LIMIT 2 OFFSET 1"),
                         [("Bob",), ("Cara",)])

    def test_null_logic(self):
        # NULL is never equal to anything, so Dan (age NULL) matches neither
        self.assertEqual(self.value("SELECT COUNT(*) FROM users WHERE age > 20 OR age <= 20"), 3)
        self.assertEqual(self.rows("SELECT name FROM users WHERE age IS NULL"), [("Dan",)])
        self.assertEqual(self.value("SELECT NULL = NULL"), None)

    def test_expressions_and_functions(self):
        self.assertEqual(self.rows("SELECT 1 + 2 * 3, 7 / 2, 7.0 / 2, 7 % 3, 'a' || 'b', -(4)"),
                         [(7, 3, 3.5, 1, "ab", -4)])
        self.assertEqual(self.value("SELECT UPPER(name) FROM users WHERE id = 1"), "ADI")
        self.assertEqual(self.value("SELECT COALESCE(email, 'none') FROM users WHERE name = 'Cara'"), "none")
        self.assertEqual(self.rows("SELECT name FROM users WHERE name LIKE 'c%'"), [("Cara",)])
        self.assertEqual(self.value("SELECT COUNT(*) FROM users WHERE id IN (1, 3, 99)"), 2)
        self.assertEqual(self.value("SELECT COUNT(*) FROM users WHERE age BETWEEN 19 AND 24"), 2)

    def test_aggregates_and_group_by(self):
        self.assertEqual(self.rows("SELECT COUNT(*), COUNT(age), SUM(age), MIN(age), MAX(age) FROM users"),
                         [(4, 3, 73, 19, 30)])
        self.assertEqual(
            self.rows("SELECT city, COUNT(*) AS n, AVG(age) FROM users GROUP BY city ORDER BY city"),
            [("Cardiff", 2, 21.5), ("London", 2, 30.0)])
        self.assertEqual(self.rows("SELECT city FROM users GROUP BY city HAVING MAX(age) > 25"),
                         [("London",)])
        self.assertEqual(self.rows("SELECT COUNT(DISTINCT city) FROM users"), [(2,)])
        self.assertEqual(self.rows("SELECT DISTINCT city FROM users ORDER BY city"),
                         [("Cardiff",), ("London",)])

    def test_update(self):
        self.assertEqual(self.db.execute("UPDATE users SET age = age + 1 WHERE city = 'Cardiff'").message,
                         "2 row(s) updated")
        self.assertEqual(self.rows("SELECT age FROM users WHERE city = 'Cardiff' ORDER BY id"), [(25,), (20,)])

    def test_delete(self):
        self.db.execute("DELETE FROM users WHERE city = 'London'")
        self.assertEqual(self.value("SELECT COUNT(*) FROM users"), 2)
        self.db.execute("DELETE FROM users")
        self.assertEqual(self.value("SELECT COUNT(*) FROM users"), 0)

    def test_constraints(self):
        cases = {
            "INSERT INTO users (name, email) VALUES ('X', 'adi@x.com')": "UNIQUE",
            "INSERT INTO users (id, name) VALUES (1, 'X')": "UNIQUE",
            "INSERT INTO users (name) VALUES (NULL)": "NOT NULL",
            "INSERT INTO users (name, age) VALUES ('X', 'old')": "type mismatch",
            "UPDATE users SET email = 'bob@x.com' WHERE id = 1": "UNIQUE",
        }
        for sql, msg in cases.items():
            with self.assertRaises(DatabaseError) as ctx:
                self.db.execute(sql)
            self.assertIn(msg, str(ctx.exception))
        self.assertEqual(self.value("SELECT COUNT(*) FROM users"), 4)   # nothing half-inserted

    def test_failed_statement_is_atomic(self):
        # the 2nd row violates UNIQUE, so the 1st must not be kept either
        with self.assertRaises(DatabaseError):
            self.db.execute("INSERT INTO users (name, email) VALUES ('New', 'new@x.com'), ('Dup', 'bob@x.com')")
        self.assertEqual(self.rows("SELECT * FROM users WHERE name = 'New'"), [])

    def test_errors(self):
        for sql in ["SELECT nope FROM users", "SELECT * FROM missing", "SELEC 1",
                    "CREATE TABLE users (a INT)", "INSERT INTO users VALUES (1)"]:
            with self.assertRaises(DatabaseError):
                self.db.execute(sql)

    def test_query_planner_uses_indexes(self):
        plan = lambda q: self.value("EXPLAIN " + q)
        self.assertIn("PRIMARY KEY", plan("SELECT * FROM users WHERE id = 2"))
        self.assertIn("autoindex_users_email", plan("SELECT * FROM users WHERE email = 'bob@x.com'"))
        self.assertIn("SCAN", plan("SELECT * FROM users WHERE city = 'London'"))
        self.db.execute("CREATE INDEX idx_city ON users (city)")
        self.assertIn("idx_city", plan("SELECT * FROM users WHERE city = 'London' AND age > 1"))
        self.assertEqual(self.rows("SELECT name FROM users WHERE city = 'London' ORDER BY name"),
                         [("Bob",), ("Dan",)])

    def test_index_stays_correct_after_changes(self):
        self.db.execute("CREATE INDEX idx_city ON users (city)")
        self.db.execute("UPDATE users SET city = 'Bristol' WHERE name = 'Adi'")
        self.db.execute("DELETE FROM users WHERE name = 'Cara'")
        self.assertEqual(self.rows("SELECT name FROM users WHERE city = 'Cardiff'"), [])
        self.assertEqual(self.rows("SELECT name FROM users WHERE city = 'Bristol'"), [("Adi",)])

    def test_transactions(self):
        self.db.execute("BEGIN")
        self.db.execute("DELETE FROM users")
        self.assertEqual(self.value("SELECT COUNT(*) FROM users"), 0)
        self.db.execute("ROLLBACK")
        self.assertEqual(self.value("SELECT COUNT(*) FROM users"), 4)

        self.db.execute_script("BEGIN; INSERT INTO users (name) VALUES ('Eve'); COMMIT;")
        self.reopen()
        self.assertEqual(self.value("SELECT COUNT(*) FROM users"), 5)

    def test_uncommitted_changes_lost_on_close(self):
        self.db.execute_script("BEGIN; DELETE FROM users;")
        self.reopen()
        self.assertEqual(self.value("SELECT COUNT(*) FROM users"), 4)

    def test_persistence(self):
        self.db.execute("CREATE INDEX idx_city ON users (city)")
        self.reopen()
        self.assertEqual(self.value("SELECT COUNT(*) FROM users"), 4)
        self.assertIn("idx_city", self.value("EXPLAIN SELECT * FROM users WHERE city = 'x'"))

    def test_drop(self):
        self.db.execute("CREATE INDEX idx_city ON users (city)")
        self.db.execute("DROP INDEX idx_city")
        self.db.execute("DROP TABLE users")
        with self.assertRaises(DatabaseError):
            self.db.execute("SELECT * FROM users")
        self.db.execute("CREATE TABLE users (x INT)")      # name is free again
        self.db.execute("DROP TABLE IF EXISTS nothing")

    def test_many_rows(self):
        self.db.execute("CREATE TABLE big (id INTEGER PRIMARY KEY, k TEXT, n INTEGER)")
        self.db.execute("CREATE INDEX idx_k ON big (k)")
        self.db.execute("BEGIN")
        for start in range(0, 5000, 500):
            values = ", ".join(f"('key{i % 100}', {i})" for i in range(start, start + 500))
            self.db.execute(f"INSERT INTO big (k, n) VALUES {values}")
        self.db.execute("COMMIT")
        self.reopen()
        self.assertEqual(self.value("SELECT COUNT(*) FROM big"), 5000)
        self.assertEqual(self.value("SELECT COUNT(*) FROM big WHERE k = 'key42'"), 50)
        self.assertEqual(self.value("SELECT SUM(n) FROM big"), sum(range(5000)))
        self.assertEqual(self.rows("SELECT n FROM big WHERE id = 4321"), [(4320,)])


if __name__ == "__main__":
    unittest.main()
