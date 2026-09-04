"""Storage layer: durable data on S3 and how a table name resolves to it.

`legs` mounts the optional catalog access paths (S3 Tables, Glue, DuckLake);
`scenarios` hold what each data domain contributes (views, partitioned
sources, prompt doc, default policies); `registry` merges them.
"""
