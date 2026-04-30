from datetime import date, timedelta

from flask import Blueprint, Response, flash, redirect, render_template, request, url_for
from openpyxl import Workbook

from .auth_helpers import login_required, roles_required
from .extensions import db
from .models import Book, LibraryLoan, LibraryMember, LibraryPayment, SubscriptionPlan

bp = Blueprint("library", __name__, url_prefix="/library")


def _loan_charge(loan: LibraryLoan) -> float:
    today = loan.return_date or date.today()
    late_days = max(0, (today - loan.due_date).days)
    late_fee = late_days * loan.late_fee_per_day
    return round(late_fee + loan.damage_fee + loan.lost_fee, 2)


@bp.route("/")
@login_required
def home():
    active_loans = LibraryLoan.query.filter_by(status="issued").count()
    due_tomorrow = LibraryLoan.query.filter(
        LibraryLoan.status == "issued", LibraryLoan.due_date == date.today() + timedelta(days=1)
    ).count()
    return render_template(
        "library/home.html",
        active_loans=active_loans,
        due_tomorrow=due_tomorrow,
        members=LibraryMember.query.filter_by(active=True).count(),
    )


@bp.route("/members", methods=["GET", "POST"])
@roles_required("admin", "manager", "librarian")
def members():
    if request.method == "POST":
        plan_id = int(request.form["subscription_plan_id"]) if request.form.get("subscription_plan_id") else None
        start_date = date.fromisoformat(request.form["subscription_start_date"]) if request.form.get("subscription_start_date") else None
        end_date = None
        if plan_id and start_date:
            plan = SubscriptionPlan.query.get(plan_id)
            end_date = start_date + timedelta(days=plan.duration_days)
        member = LibraryMember(
            full_name=request.form["full_name"].strip(),
            phone=request.form["phone"].strip(),
            email=request.form.get("email", "").strip() or None,
            address=request.form.get("address", "").strip() or None,
            subscription_plan_id=plan_id,
            subscription_start_date=start_date,
            subscription_end_date=end_date,
            card_number=request.form.get("card_number", "").strip() or None,
            active=True if request.form.get("active") else False,
        )
        db.session.add(member)
        db.session.commit()
        flash("Member added.", "success")
        return redirect(url_for("library.members"))
    return render_template(
        "library/members.html",
        members=LibraryMember.query.order_by(LibraryMember.full_name).all(),
        plans=SubscriptionPlan.query.filter_by(active=True).order_by(SubscriptionPlan.name).all(),
    )


@bp.route("/books", methods=["GET", "POST"])
@roles_required("admin", "manager", "librarian")
def books():
    if request.method == "POST":
        total = int(request.form["total_copies"])
        book = Book(
            title=request.form["title"].strip(),
            author=request.form["author"].strip(),
            category=request.form.get("category", "").strip() or None,
            shelf_no=request.form["shelf_no"].strip(),
            total_copies=total,
            available_copies=total,
        )
        db.session.add(book)
        db.session.commit()
        flash("Book added.", "success")
        return redirect(url_for("library.books"))
    return render_template("library/books.html", books=Book.query.order_by(Book.title).all())


@bp.route("/plans", methods=["GET", "POST"])
@roles_required("admin", "manager", "librarian")
def plans():
    if request.method == "POST":
        plan = SubscriptionPlan(
            name=request.form["name"].strip(),
            duration_days=int(request.form["duration_days"]),
            weekly_reissue_fee_per_book=float(request.form["weekly_reissue_fee_per_book"]),
            late_fee_per_day=float(request.form["late_fee_per_day"]),
            active=True if request.form.get("active") else False,
        )
        db.session.add(plan)
        db.session.commit()
        flash("Plan added.", "success")
        return redirect(url_for("library.plans"))
    return render_template(
        "library/plans.html", plans=SubscriptionPlan.query.order_by(SubscriptionPlan.duration_days).all()
    )


@bp.route("/loans", methods=["GET", "POST"])
@roles_required("admin", "manager", "librarian")
def loans():
    if request.method == "POST":
        member = LibraryMember.query.get(int(request.form["member_id"]))
        book = Book.query.get(int(request.form["book_id"]))
        if not book or book.available_copies < 1:
            flash("Book is not available.", "error")
            return redirect(url_for("library.loans"))
        weekly_fee = float(request.form.get("weekly_fee_per_book") or 10)
        late_fee = float(request.form.get("late_fee_per_day") or 5)
        issue_date = date.fromisoformat(request.form["issue_date"])
        due_date = date.fromisoformat(request.form["due_date"])
        loan = LibraryLoan(
            member_id=member.id,
            book_id=book.id,
            issue_date=issue_date,
            due_date=due_date,
            weekly_fee_per_book=weekly_fee,
            late_fee_per_day=late_fee,
            status="issued",
        )
        book.available_copies -= 1
        db.session.add(loan)
        db.session.commit()
        flash("Book issued.", "success")
        return redirect(url_for("library.loans"))
    return render_template(
        "library/loans.html",
        members=LibraryMember.query.filter_by(active=True).order_by(LibraryMember.full_name).all(),
        books=Book.query.order_by(Book.title).all(),
        loans=LibraryLoan.query.order_by(LibraryLoan.created_at.desc()).all(),
    )


@bp.route("/loans/<int:loan_id>/reissue", methods=["POST"])
@roles_required("admin", "manager", "librarian")
def reissue(loan_id):
    loan = LibraryLoan.query.get_or_404(loan_id)
    if loan.status != "issued":
        return redirect(url_for("library.loans"))
    loan.reissue_count += 1
    loan.due_date = loan.due_date + timedelta(days=7)
    db.session.commit()
    flash("Loan reissued for 7 days.", "success")
    return redirect(url_for("library.loans"))


@bp.route("/loans/<int:loan_id>/return", methods=["POST"])
@roles_required("admin", "manager", "librarian")
def return_book(loan_id):
    loan = LibraryLoan.query.get_or_404(loan_id)
    if loan.status != "issued":
        return redirect(url_for("library.loans"))
    loan.return_date = date.fromisoformat(request.form["return_date"])
    loan.damage_fee = float(request.form.get("damage_fee") or 0)
    loan.lost_fee = float(request.form.get("lost_fee") or 0)
    loan.total_charge = _loan_charge(loan)
    loan.status = "returned"
    loan.book.available_copies += 1 if loan.lost_fee == 0 else 0
    db.session.commit()
    flash("Book returned and charges calculated.", "success")
    return redirect(url_for("library.loans"))


@bp.route("/payments", methods=["GET", "POST"])
@roles_required("admin", "manager", "librarian")
def payments():
    if request.method == "POST":
        payment = LibraryPayment(
            member_id=int(request.form["member_id"]),
            amount=float(request.form["amount"]),
            payment_type=request.form["payment_type"],
            reference=request.form.get("reference", "").strip() or None,
            note=request.form.get("note", "").strip() or None,
        )
        db.session.add(payment)
        db.session.commit()
        flash("Payment recorded.", "success")
        return redirect(url_for("library.payments"))
    return render_template(
        "library/payments.html",
        members=LibraryMember.query.filter_by(active=True).order_by(LibraryMember.full_name).all(),
        payments=LibraryPayment.query.order_by(LibraryPayment.created_at.desc()).all(),
    )


@bp.route("/stats/export")
@roles_required("admin", "manager", "librarian")
def export_stats():
    wb = Workbook()
    ws = wb.active
    ws.title = "Library"
    ws.append(["Loan ID", "Member", "Book", "Issue Date", "Due Date", "Status", "Charge"])
    for loan in LibraryLoan.query.order_by(LibraryLoan.created_at.desc()).all():
        ws.append(
            [
                loan.id,
                loan.member.full_name,
                loan.book.title,
                loan.issue_date.isoformat(),
                loan.due_date.isoformat(),
                loan.status,
                loan.total_charge,
            ]
        )
    from io import BytesIO

    output = BytesIO()
    wb.save(output)
    output.seek(0)
    return Response(
        output.getvalue(),
        headers={
            "Content-Disposition": 'attachment; filename="library_stats.xlsx"',
            "Content-Type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        },
    )
