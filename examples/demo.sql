-- Run with:  python3 -m mydb demo.db < examples/demo.sql

CREATE TABLE students (
    id      INTEGER PRIMARY KEY,
    name    TEXT NOT NULL,
    email   TEXT UNIQUE,
    course  TEXT,
    score   REAL
);

INSERT INTO students (name, email, course, score) VALUES
    ('Adi',    'adi@uni.ac.uk',    'Data Science', 88.5),
    ('Priya',  'priya@uni.ac.uk',  'Data Science', 92.0),
    ('Tom',    'tom@uni.ac.uk',    'Computing',    74.0),
    ('Sara',   'sara@uni.ac.uk',   'Computing',    81.5),
    ('Leo',    'leo@uni.ac.uk',    'Maths',        67.0),
    ('Mia',    NULL,               'Maths',        NULL);

SELECT * FROM students;

SELECT name, score FROM students WHERE score > 80 ORDER BY score DESC;

SELECT course, COUNT(*) AS students, ROUND(AVG(score), 1) AS avg_score
FROM students GROUP BY course ORDER BY avg_score DESC;

CREATE INDEX idx_course ON students (course);
EXPLAIN SELECT * FROM students WHERE course = 'Maths';
EXPLAIN SELECT * FROM students WHERE name = 'Leo';

UPDATE students SET score = 70 WHERE name = 'Mia';

BEGIN;
DELETE FROM students;
SELECT COUNT(*) AS during_transaction FROM students;
ROLLBACK;
SELECT COUNT(*) AS after_rollback FROM students;
