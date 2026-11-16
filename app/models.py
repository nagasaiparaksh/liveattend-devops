"""Database models (PostgreSQL in production, SQLite for tests)."""
from datetime import datetime, timezone

from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash

db = SQLAlchemy()


def now():
    return datetime.now(timezone.utc)


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(40), unique=True, nullable=False)
    pw_hash = db.Column(db.String(256), nullable=False)
    created = db.Column(db.DateTime(timezone=True), default=now)
    courses = db.relationship("Course", backref="owner", lazy=True)

    def set_password(self, pw):
        self.pw_hash = generate_password_hash(pw)

    def check_password(self, pw):
        return check_password_hash(self.pw_hash, pw)


class Course(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(20), nullable=False)
    name = db.Column(db.String(120), nullable=False)
    owner_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    created = db.Column(db.DateTime(timezone=True), default=now)
    sessions = db.relationship("ClassSession", backref="course", lazy=True, order_by="ClassSession.started.desc()",
                               cascade="all, delete-orphan")


class ClassSession(db.Model):
    """One lecture / lab. While open, students can mark attendance with the rotating QR code."""
    id = db.Column(db.Integer, primary_key=True)
    course_id = db.Column(db.Integer, db.ForeignKey("course.id"), nullable=False)
    title = db.Column(db.String(120), nullable=False)
    secret = db.Column(db.String(64), nullable=False)      # signs the rotating QR tokens, never leaves the server
    started = db.Column(db.DateTime(timezone=True), default=now)
    ended = db.Column(db.DateTime(timezone=True))
    records = db.relationship("Record", backref="session", lazy=True, order_by="Record.marked_at",
                              cascade="all, delete-orphan")

    @property
    def is_open(self):
        return self.ended is None


class Record(db.Model):
    __table_args__ = (db.UniqueConstraint("session_id", "reg_no", name="one_mark_per_student"),)
    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey("class_session.id"), nullable=False)
    reg_no = db.Column(db.String(20), nullable=False)
    name = db.Column(db.String(60), nullable=False)
    marked_at = db.Column(db.DateTime(timezone=True), default=now)
    ip = db.Column(db.String(64), default="")
