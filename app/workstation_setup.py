"""Seed workstation examples once; subsequent configuration belongs to admins."""
from .extensions import db
from .models import InitialSetupState, Workstation


def initialize_workstations():
    key = "workstations_initialized"
    if db.session.get(InitialSetupState, key):
        return
    if not Workstation.query.first():
        for index, (slug, name) in enumerate((("kitchen", "Kitchen"), ("barista", "Barista Counter")), 1):
            db.session.add(Workstation(slug=slug, name=name, active=True, display_order=index))
    db.session.add(InitialSetupState(key=key))
    db.session.commit()
