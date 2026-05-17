import os
import random
import string
from datetime import date, timedelta
from io import BytesIO
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from flask import Blueprint, Response, current_app, flash, redirect, render_template, request, url_for
from PIL import Image, ImageDraw, ImageFont
from openpyxl import Workbook
from werkzeug.utils import secure_filename

from .auth_helpers import login_required, roles_required
from .extensions import db
from .models import Book, LibraryAuthor, LibraryLoan, LibraryMember, LibraryPayment, SubscriptionPlan

bp = Blueprint("library", __name__, url_prefix="/library")
BOOK_GENRES = [
    "Fiction", "Non-Fiction", "Mystery", "Thriller", "Romance", "Fantasy", "Science Fiction",
    "Horror", "Historical", "Biography", "Autobiography", "Self-Help", "Philosophy", "Poetry",
    "Drama", "Children", "Young Adult", "Travel", "Health", "Business", "Technology",
    "Politics", "Religion", "Spirituality", "Comics", "Graphic Novel", "Crime", "Adventure",
    "Education", "Reference", "Classic", "Satire", "Cookbook",
]


def _loan_charge(loan: LibraryLoan) -> float:
    today = loan.return_date or date.today()
    late_days = max(0, (today - loan.due_date).days)
    late_fee = late_days * loan.late_fee_per_day
    return round(late_fee + loan.damage_fee + loan.lost_fee, 2)


def _generate_member_code() -> str:
    while True:
        code = "BBL-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=8))
        if not LibraryMember.query.filter_by(member_code=code).first():
            return code


def _build_member_card(member: LibraryMember):
    canvas = Image.new("RGB", (960, 540), "#f4efe6")
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.load_default()
    draw.rectangle((20, 20, 940, 520), outline="#3f2b1d", width=4)
    logo_path = os.path.join(bp.root_path, "..", "static", "images", "cafe-logo.png")
    logo_path = os.path.abspath(logo_path)
    if os.path.exists(logo_path):
        logo = Image.open(logo_path).convert("RGBA").resize((130, 130))
        canvas.paste(logo, (50, 45), logo)
    draw.text((220, 62), "Brownberries Library Membership Card", fill="#2f2118", font=font)
    draw.text((220, 100), f"Member ID: {member.member_code or '-'}", fill="#2f2118", font=font)
    draw.text((220, 130), f"Name: {member.full_name}", fill="#2f2118", font=font)
    draw.text((220, 160), f"Phone: {member.phone}", fill="#2f2118", font=font)
    draw.text((220, 190), f"Email: {member.email or '-'}", fill="#2f2118", font=font)
    draw.text((220, 220), f"Plan: {member.subscription_plan.name if member.subscription_plan else '-'}", fill="#2f2118", font=font)
    draw.text((220, 250), f"Valid Till: {member.subscription_end_date or '-'}", fill="#2f2118", font=font)
    out = BytesIO()
    canvas.save(out, format="PNG")
    out.seek(0)
    return out


def _save_uploaded_library_doc(file_obj, prefix: str):
    if not file_obj or not file_obj.filename:
        return None
    ext = os.path.splitext(secure_filename(file_obj.filename))[1].lower() or ".bin"
    filename = f"{prefix}-{''.join(random.choices(string.ascii_lowercase + string.digits, k=10))}{ext}"
    root = current_app.config["UPLOADS_ROOT"]
    folder = os.path.join(root, "library_docs")
    os.makedirs(folder, exist_ok=True)
    full_path = os.path.join(folder, filename)
    file_obj.save(full_path)
    return os.path.relpath(full_path, root)


def _send_sms(phone: str, body: str):
    sid = os.environ.get("TWILIO_ACCOUNT_SID", "").strip()
    token = os.environ.get("TWILIO_AUTH_TOKEN", "").strip()
    from_number = os.environ.get("TWILIO_FROM_NUMBER", "").strip()
    if not (sid and token and from_number):
        return False, "SMS skipped: Twilio env vars not configured."
    to_number = phone if phone.startswith("+") else f"+91{phone}"
    payload = urlencode({"From": from_number, "To": to_number, "Body": body}).encode("utf-8")
    endpoint = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
    req = Request(endpoint, data=payload)
    import base64

    auth = base64.b64encode(f"{sid}:{token}".encode("utf-8")).decode("utf-8")
    req.add_header("Authorization", f"Basic {auth}")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urlopen(req, timeout=15) as resp:
            return (200 <= resp.status < 300), f"Twilio status: {resp.status}"
    except Exception as exc:
        return False, f"SMS failed: {exc}"


def _process_due_reminders():
    sid = os.environ.get("TWILIO_ACCOUNT_SID", "").strip()
    token = os.environ.get("TWILIO_AUTH_TOKEN", "").strip()
    from_number = os.environ.get("TWILIO_FROM_NUMBER", "").strip()
    if not (sid and token and from_number):
        return 0, 0
    tomorrow = date.today() + timedelta(days=1)
    loans = (
        LibraryLoan.query.join(LibraryMember, LibraryLoan.member_id == LibraryMember.id)
        .filter(
            LibraryLoan.status == "issued",
            LibraryLoan.due_date == tomorrow,
            db.or_(LibraryLoan.due_reminder_sent_on.is_(None), LibraryLoan.due_reminder_sent_on != date.today()),
            LibraryMember.active.is_(True),
        )
        .all()
    )
    sent = 0
    failed = 0
    for loan in loans:
        msg = (
            f"Hi {loan.member.full_name}, reminder from Brownberries Library: "
            f'Please return/renew "{loan.book.title}" by {loan.due_date.isoformat()}.'
        )
        ok, _ = _send_sms(loan.member.phone, msg)
        if ok:
            loan.due_reminder_sent_on = date.today()
            sent += 1
        else:
            failed += 1
    db.session.commit()
    return sent, failed


@bp.route("/")
@login_required
def home():
    _process_due_reminders()
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
    status = (request.args.get("status") or "").strip().lower()
    members_query = LibraryMember.query
    if status == "active":
        members_query = members_query.filter_by(active=True)
    elif status == "inactive":
        members_query = members_query.filter_by(active=False)
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
            member_code=_generate_member_code(),
            subscription_plan_id=plan_id,
            subscription_start_date=start_date,
            subscription_end_date=end_date,
            card_number=request.form.get("card_number", "").strip() or None,
            govt_id_image_path=_save_uploaded_library_doc(request.files.get("govt_id_image"), "library-member-id"),
            active=True if request.form.get("active") else False,
        )
        db.session.add(member)
        db.session.commit()
        flash("Member added.", "success")
        return redirect(url_for("library.members"))
    return render_template(
        "library/members.html",
        members=members_query.order_by(LibraryMember.full_name).all(),
        plans=SubscriptionPlan.query.filter_by(active=True).order_by(SubscriptionPlan.name).all(),
        selected_status=status,
    )


@bp.route("/members/documents", methods=["GET", "POST"])
@roles_required("admin", "manager", "librarian")
def member_documents():
    if request.method == "POST":
        member_id = int(request.form.get("member_id") or 0)
        member = LibraryMember.query.get_or_404(member_id)
        path = _save_uploaded_library_doc(request.files.get("govt_id_image"), "library-member-id")
        if not path:
            flash("Please select a Govt ID file.", "error")
            return redirect(url_for("library.member_documents"))
        member.govt_id_image_path = path
        db.session.commit()
        flash("Member document uploaded.", "success")
        return redirect(url_for("library.member_documents"))
    return render_template(
        "library/member_documents.html",
        members=LibraryMember.query.order_by(LibraryMember.full_name.asc()).all(),
    )


@bp.route("/members/<int:member_id>/govt-id")
@roles_required("admin", "manager", "librarian")
def member_govt_id(member_id):
    member = LibraryMember.query.get_or_404(member_id)
    if not member.govt_id_image_path:
        flash("No Govt ID file uploaded for this member.", "error")
        return redirect(url_for("library.members"))
    full_path = os.path.join(current_app.config["UPLOADS_ROOT"], member.govt_id_image_path)
    if not os.path.exists(full_path):
        flash("Govt ID file is missing on disk.", "error")
        return redirect(url_for("library.members"))
    with open(full_path, "rb") as f:
        data = f.read()
    return Response(
        data,
        headers={
            "Content-Disposition": f'attachment; filename="{os.path.basename(full_path)}"',
            "Content-Type": "application/octet-stream",
        },
    )


@bp.route("/members/<int:member_id>/card")
@roles_required("admin", "manager", "librarian")
def member_card(member_id):
    member = LibraryMember.query.get_or_404(member_id)
    output = _build_member_card(member)
    filename = f"{member.full_name.lower().replace(' ', '-')}-library-card.png"
    return Response(
        output.getvalue(),
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Type": "image/png",
        },
    )


@bp.route("/books", methods=["GET", "POST"])
@roles_required("admin", "manager", "librarian")
def books():
    selected_book_id = request.args.get("book_id", type=int)
    if request.method == "POST":
        action = request.form.get("action", "create")
        if action == "create":
            total = int(request.form["total_copies"])
            author_name = request.form.get("author", "").strip()
            if not author_name:
                flash("Author is required.", "error")
                return redirect(url_for("library.books"))
            book = Book(
                title=request.form["title"].strip(),
                author=author_name,
                genre=request.form.get("genre", "").strip() or None,
                category=request.form.get("category", "").strip() or None,
                shelf_no=request.form["shelf_no"].strip(),
                total_copies=total,
                available_copies=total,
            )
            existing_author = LibraryAuthor.query.filter(
                db.func.lower(LibraryAuthor.name) == author_name.lower()
            ).first()
            if not existing_author:
                db.session.add(LibraryAuthor(name=author_name, active=True))
            db.session.add(book)
            db.session.commit()
            flash("Book added.", "success")
            return redirect(url_for("library.books"))
        if action == "update":
            book = Book.query.get_or_404(int(request.form["book_id"]))
            prev_total = book.total_copies
            new_total = int(request.form["total_copies"])
            delta = new_total - prev_total
            author_name = request.form.get("author", "").strip()
            if not author_name:
                flash("Author is required.", "error")
                return redirect(url_for("library.books", q=request.args.get("q", ""), book_id=book.id))
            book.title = request.form["title"].strip()
            book.author = author_name
            book.genre = request.form.get("genre", "").strip() or None
            book.category = request.form.get("category", "").strip() or None
            book.shelf_no = request.form["shelf_no"].strip()
            book.total_copies = new_total
            book.available_copies = max(0, book.available_copies + delta)
            existing_author = LibraryAuthor.query.filter(
                db.func.lower(LibraryAuthor.name) == author_name.lower()
            ).first()
            if not existing_author:
                db.session.add(LibraryAuthor(name=author_name, active=True))
            db.session.commit()
            flash("Book updated.", "success")
            return redirect(url_for("library.books", q=request.args.get("q", ""), book_id=book.id))
        if action == "delete":
            book = Book.query.get_or_404(int(request.form["book_id"]))
            active_loans = LibraryLoan.query.filter_by(book_id=book.id, status="issued").count()
            if active_loans > 0:
                flash("Cannot delete book with active issued loans.", "error")
                return redirect(url_for("library.books", q=request.args.get("q", ""), book_id=book.id))
            db.session.delete(book)
            db.session.commit()
            flash("Book deleted.", "success")
            return redirect(url_for("library.books", q=request.args.get("q", "")))
    q = (request.args.get("q") or "").strip()
    query = Book.query
    if q:
        like = f"%{q}%"
        query = query.filter(
            db.or_(
                Book.title.ilike(like),
                Book.author.ilike(like),
                Book.genre.ilike(like),
                Book.shelf_no.ilike(like),
            )
        )
    books_list = query.order_by(Book.title).all()
    selected_book = None
    if selected_book_id:
        selected_book = next((b for b in books_list if b.id == selected_book_id), None)
        if not selected_book:
            selected_book = Book.query.get(selected_book_id)
    if not selected_book and books_list:
        selected_book = books_list[0]
    return render_template(
        "library/books.html",
        books=books_list,
        selected_book=selected_book,
        authors=LibraryAuthor.query.filter_by(active=True).order_by(LibraryAuthor.name.asc()).all(),
        book_genres=BOOK_GENRES,
        q=q,
    )


@bp.route("/authors", methods=["GET", "POST"])
@roles_required("admin", "manager", "librarian")
def authors():
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        if not name:
            flash("Author name is required.", "error")
            return redirect(url_for("library.authors"))
        existing = LibraryAuthor.query.filter(db.func.lower(LibraryAuthor.name) == name.lower()).first()
        if existing:
            existing.active = True
            db.session.commit()
            flash("Author already exists. Marked active.", "success")
            return redirect(url_for("library.authors"))
        db.session.add(LibraryAuthor(name=name, active=True))
        db.session.commit()
        flash("Author added.", "success")
        return redirect(url_for("library.authors"))
    return render_template(
        "library/authors.html",
        authors=LibraryAuthor.query.order_by(LibraryAuthor.name.asc()).all(),
    )


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
    status = (request.args.get("status") or "").strip().lower()
    loans_query = LibraryLoan.query
    if status == "issued":
        loans_query = loans_query.filter(LibraryLoan.status == "issued")
    elif status == "due_tomorrow":
        loans_query = loans_query.filter(
            LibraryLoan.status == "issued",
            LibraryLoan.due_date == date.today() + timedelta(days=1),
        )
    elif status:
        loans_query = loans_query.filter(LibraryLoan.status == status)

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
        loans=loans_query.order_by(LibraryLoan.created_at.desc()).all(),
        selected_status=status,
    )


@bp.route("/loans/<int:loan_id>/due-date", methods=["POST"])
@roles_required("admin", "manager", "librarian")
def update_due_date(loan_id):
    loan = LibraryLoan.query.get_or_404(loan_id)
    if loan.status != "issued":
        flash("Only issued loans can be updated.", "error")
        return redirect(url_for("library.loans"))
    due_date_val = request.form.get("due_date", "").strip()
    if not due_date_val:
        flash("Due date is required.", "error")
        return redirect(url_for("library.loans"))
    new_due_date = date.fromisoformat(due_date_val)
    if new_due_date < loan.issue_date:
        flash("Due date cannot be before issue date.", "error")
        return redirect(url_for("library.loans"))
    loan.due_date = new_due_date
    loan.due_reminder_sent_on = None
    db.session.commit()
    flash("Due date updated.", "success")
    return redirect(url_for("library.loans"))


@bp.route("/alerts/send-due-reminders", methods=["POST"])
@roles_required("admin", "manager", "librarian")
def send_due_reminders():
    sent, failed = _process_due_reminders()
    flash(f"Due reminders processed. Sent: {sent}, Failed/Skipped: {failed}.", "success" if sent else "error")
    return redirect(url_for("library.loans"))


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
    loan.book.available_copies = min(loan.book.total_copies, loan.book.available_copies + 1)
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
