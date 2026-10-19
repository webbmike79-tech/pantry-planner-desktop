# Vision Benchmark Fixtures

This directory contains the five test fixtures for evaluating storage scan vision accuracy, anti-confabulation, and hallucination rates across AI providers.

## Photos and Storage Locations

| Photo Key | Fixture File | Represented Storage Location | API `location` Argument |
| :--- | :--- | :--- | :--- |
| `spice_rack` | `20261018_182811.jpg` | Spice Rack / Wall Shelves | `Cupboard / Pantry` |
| `pantry` | `20261018_182817.jpg` | Pantry Shelves | `Cupboard / Pantry` |
| `fridge` | `20261018_182847.jpg` | Refrigerator Main Shelves | `Fridge` |
| `fridge_door` | `20261018_182856.jpg` | Refrigerator Door Racks | `Fridge` |
| `freezer` | `20261018_182909.jpg` | Freezer Shelves / Baskets | `Freezer` |

Ground truth below is Fenrir's read of the photos, corrected by Mike — his word is final.

The source manifest is defined in `ground_truth.json`. Photo binaries are local test fixtures and ignored from git version control.
