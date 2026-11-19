"""The chair types a buyer can ask for (operator list, 2026-11-19).

One list for the inquiry forms (template global `chair_types`), the server
check in `inventory.create_inquiry`, and the site chat's `type` step.
"""
CHAIR_TYPES = (
    "Banquet (stackable)",
    "Church chairs",
    "Folding with pads",
    "Metal folding",
    "Chiavari",
    "Other / not sure",
)
