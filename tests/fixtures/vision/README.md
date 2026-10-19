# Vision Benchmark Fixtures

This directory contains the five test fixtures for evaluating storage scan vision accuracy, anti-confabulation, and hallucination rates across AI providers.

Source fixture images are located in `C:\Users\ekim_\Downloads\pantry test pics`.

## Photos and Storage Locations

| Photo Fixture File | Camera Original | Location / Shelf | API `location` Argument | Key Content Identified |
| :--- | :--- | :--- | :--- | :--- |
| `879_23_t1ce (1).jpg` | `20261018_182811.jpg` | **Spice Rack** | `Cupboard / Pantry` | Slap Ya Mama Cajun Seasoning, Thundering Longhorn Rub, McCormick Sweet Onion Bacon Seasoning, Tajín, Cayenne, Black Pepper |
| `880_24_bwtv.jpg` | `20261018_182817.jpg` | **Pantry Shelves** | `Cupboard / Pantry` | Clabber Girl Baking Powder, Mission Pride Peaches, Campbell's Soup, Lipton Iced Tea, White Rice, canned beans and vegetables |
| `881_25_coyv.jpg` | `20261018_182847.jpg` | **Fridge (Main Shelves)** | `Fridge` | Heinz Tomato Ketchup, Taco Casa Hot Sauce, Egg carton, Ground Beef pack, Chocolate Chip Cookies pack, beverage pitcher |
| `882_26_umy5.jpg` | `20261018_182856.jpg` | **Fridge Door** | `Fridge` | Mrs. Renfro's Ghost Pepper Salsa, Kraft Salad Dressing, Miracle Whip, Louisiana Tartar Sauce, BBQ Sauce, Cholula Hot Sauce |
| `883_27_s2ng.jpg` | `20261018_182909.jpg` | **Freezer** | `Freezer` | Tai Pei Frozen Meal, Neapolitan Ice Cream Sandwiches, frozen vegetables, bread loaf, ice cube trays |

Ground truth below is Fenrir's read of the photos, corrected by Mike — his word is final.

The source manifest is defined in `ground_truth.json`. Photo binaries are local test fixtures and ignored from git version control.
