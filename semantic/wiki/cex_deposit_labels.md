# cex_deposit_labels — notes

- SYNTHETIC sample data standing in for a commercial attribution feed; never
  present a label from it as real-world attribution.
- Join to the ledger on `lower(address)`.
- A row means "this address is a deposit address of that exchange" — the
  cash-out hop. A transfer landing here is where funds leave the chain.
