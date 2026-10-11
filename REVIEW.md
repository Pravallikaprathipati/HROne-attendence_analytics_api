# REVIEW.md

List every defect found in the starter `app/main.py` (helpers and endpoints). For each one:

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `list_employees` | Pagination used `page * page_size` instead of `(page - 1) * page_size`, and `total` counted the entire collection instead of the filtered set. | Page 2 skipped too many rows and totals did not match the filter. | Corrected pagination and applied the department filter to `count_documents()`. |
| 2 | `punch_in` | Duplicate punch-ins for the same employee/date were not protected by a database uniqueness guarantee, and status validation allowed invalid values. | Repeating a punch-in for the same record caused duplicates. | Added unique indexes and rejected non-presence statuses. |
| 3 | `compute_late_minutes` / `attendance_date_for_punch_in` | The starter ignored IST overnight-shift rules and compared naive datetimes to local times incorrectly. | Night shifts were dated incorrectly and late minutes were wrong. | Compute the attendance date using IST + overnight shift logic and normalize datetimes to UTC. |
| 4 | `punch_out` | It did not validate that `punch_out` was after `punch_in`, within 24 hours, or already closed. | Bad inputs were accepted and race conditions could overwrite state. | Added strict validation and atomic update checks with `punch_out is None`. |
| 5 | `list_attendance` | The endpoint loaded records without proper sort/index-aware filtering and did not validate `date_from > date_to`. | Requests with inverted date ranges were accepted. | Added validation and used sort/index-friendly filtering with `count_documents()`. |
| 6 | `regularize_attendance` | The starter had no correction logic at all and no audit trail. | Manual corrections silently lost history or did not recompute values. | Added correction validation, recomputation of derived fields, and a single appended history entry. |
| 7 | Analytics endpoints | No real analytics logic existed and the code used raw data instead of stored derived fields. | Monthly summaries, department totals, and trends were wrong or absent. | Implemented monthly, department, leaderboard, and trend logic based on the stored values and working-day rules. |
| 8 | `/admin/explain/{endpoint}` | The explanation endpoint was not implemented. | The grader could not inspect the main Mongo query plan. | Added a MongoDB explain endpoint for the relevant collection and pipeline. |

Not a defect: using `created_at` as a server-side timestamp is correct because the contract explicitly says the API sets it and it is not client-supplied. The MongoDB `_id` is intentionally kept internal and never exposed by the API.
