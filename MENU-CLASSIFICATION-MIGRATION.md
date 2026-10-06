# Menu classification migration

This branch separates menu placement, serving hours, customer visibility and preparation workstation. Inventory categories are unchanged.

## Data safety

- Adds nullable `menu_item.serving_hours` and `customer_visible`; startup fills only NULL values using the previous rules.
- Breakfast alone becomes `breakfast`; Breakfast plus any other category becomes `both`; everything else becomes `regular`.
- Customer visibility preserves the previous public-category boundary, separately from availability and deletion flags.
- Existing category IDs/JSON, subcategory IDs and navigation assignments are not rewritten or deleted.
- Both means the union of Cafe Settings' two windows, intersected with workstation hours. Browsing ignores serving windows, but still respects visibility, availability and deletion.
- Newly created menu items retain an Other legacy FK for schema compatibility. Their explicit visibility and hours are authoritative.
- Statistics group by the current assigned navigation path, not historical category membership. Existing sales and payment calculations remain unchanged. An item is counted in one location, not split across legacy categories.
- Legacy category helpers and CRUD routes remain for compatibility, but no editor exposes them. Database removal is a separate future migration.

## Verification

Run with the project's Python environment:

```
python -m unittest discover -s tests -q
python scripts/audit_menu_classification.py --instance PATH_TO_BACKUP_INSTANCE
python scripts/audit_performance.py --instance PATH_TO_BACKUP_INSTANCE --output PATH_OUTSIDE_REPO --compare-ref 1051525 --check-js
```

Both audit scripts operate on disposable database copies. Never point a development server at the production instance directory.

## Manual acceptance

1. In Menu Management, confirm there is no legacy category editor. Menu Navigation retains its existing main categories, subcategories and defaults.
2. Edit an item's hours; save individually and with Save All. Reload and verify the selected value, location and customer-facing checkbox.
3. Add a customer-facing item with a navigation location. Confirm it appears in menu browsing even outside its serving hours. Hide it and confirm it disappears from customer views.
4. Test breakfast-only, regular-only and both at opening, overlap, noon and closing boundaries on table QR, staff ordering and Android staff ordering. Verify closed workstations still block ordering.
5. Change availability within one navigation location and verify items elsewhere are untouched. Old open availability forms with legacy filters must request a reload rather than update all items.
6. Filter statistics by navigation path; verify charts, item tables and Excel export agree, and unfiltered sales/payments are unchanged.
7. Confirm SOPs, responsibility assignments, inventory categories and existing orders are unchanged.

## Deployment and rollback

Not deployed by these changes. Before deployment, take a verified current AWS backup and record the production Git revision. Test this branch against that backup before restarting production.

The schema change is additive: older application code can read the database, but it ignores the new fields. Consequently **a code-only rollback does not preserve serving-hours or visibility edits made after deployment**, and newly created Other-backed items would be hidden by old code. Before reverting, freeze writes and prepare a reviewed compatibility mapping for those edits; do not restore an old database over newer orders/payments. Retain the new fields and pre-deployment backup until all consumers have migrated. The retired Windows server is not a current database fallback.
