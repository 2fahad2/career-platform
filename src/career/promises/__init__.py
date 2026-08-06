"""The three things the store SELLS that no code implemented.

Each module here exists because a sentence on a Salla product page is a
contract with somebody who has already paid, and until now there was nothing
behind these three: the 72-hour start guarantee had no clock, the career
session had no ledger, and the founder price lock had no record — and worse,
its absence turned the promise into an outage, because a founder renewing at
their locked price hit the §09 triple match and was refused with a TERMINAL
`AMOUNT_MISMATCH` that nothing ever re-read.

They live together, apart from the machinery that serves them, for one
reason: the wording is the specification. Every rule in this package quotes
the Arabic sentence it implements, so the day the store page changes the diff
lands here and not in six modules that each remembered it differently.
"""
