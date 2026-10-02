# My Database

A SQL database implemented from scratch in Python, with storage, indexing and query execution built without external database libraries.

## Overview

This project implements a simplified relational database using fixed-size pages and B+ trees. It supports SQL statements, table creation, indexes, query planning and transactional behaviour.

## Core capabilities

- table creation and schema definitions
- SQL query execution with filtering and sorting
- indexes for lookup acceleration
- transaction handling with rollback support
- crash-safe journal logic

## Tech Stack

- Python
- SQL
- data structures and storage engine design

## How to run

```bash
python3 -m mydb school.db
python3 -m mydb demo.db < examples/demo.sql
```

You can also use it from Python by creating a database object and executing SQL commands.

## Project structure

```text
.
├── mydb/
├── tests/
├── examples/
├── README.md
└── supporting engine files
```

