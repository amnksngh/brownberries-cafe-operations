"""Conservative hard deletion: never remove records used by business history."""
from .extensions import db


def menu_deletion_blockers(item_id):
    blockers = []
    for table in db.metadata.sorted_tables:
        # The independent operational master and its SOPs can survive unlinking.
        if table.name == "operational_item":
            continue
        for column in table.columns:
            if any(fk.target_fullname == "menu_item.id" for fk in column.foreign_keys):
                count = db.session.execute(db.select(db.func.count()).select_from(table).where(column == item_id)).scalar_one()
                if count:
                    labels = {"cafe_order_item": "order lines", "cafe_feedback_item": "feedback records",
                              "inventory_recipe": "recipes / display SOPs",
                              "operational_daily_ownership_override": "responsibility history"}
                    blockers.append(f"{count} {labels.get(table.name, table.name.replace('_', ' '))}")
    return blockers
