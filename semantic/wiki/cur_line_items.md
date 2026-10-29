# cur_line_items — notes

- "Cost" means `line_item_unblended_cost` unless the user says amortized / net;
  the registered metrics use unblended, so answer in the same basis.
- Bound `billing_period` (YYYY-MM). A month-over-month question needs both
  periods in one statement.
- Credits and refunds are separate line item types; check
  `line_item_line_item_type` before summing across them.
