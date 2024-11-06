import datetime

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import Index
from sqlalchemy.dialects.sqlite import JSON
from sqlalchemy.orm import configure_mappers

db = SQLAlchemy()


class Config(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String, unique=True, nullable=False)
    value = db.Column(db.String, nullable=False)


class Site(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    uuid = db.Column(db.String, unique=True, nullable=False)
    name = db.Column(db.String, nullable=False)
    url = db.Column(db.String)
    description = db.Column(db.Text)
    rating = db.Column(db.Float)
    network = db.Column(db.String)
    parent = db.Column(db.String)
    logo = db.Column(db.String)
    home_directory = db.Column(db.String)
    scenes = db.relationship('Scene', backref='site', lazy=True)


class Scene(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    site_id = db.Column(db.Integer, db.ForeignKey('site.id'), nullable=False)
    title = db.Column(db.String, nullable=False)
    date = db.Column(db.String)
    duration = db.Column(db.Integer)
    image = db.Column(db.String)
    performers = db.Column(JSON)
    status = db.Column(db.String)
    local_path = db.Column(db.String)
    year = db.Column(db.Integer)
    episode_number = db.Column(db.Integer)
    slug = db.Column(db.String)
    overview = db.Column(db.Text)
    credits = db.Column(JSON)
    release_date_utc = db.Column(db.String)
    images = db.Column(JSON)
    trailer = db.Column(db.String)
    genres = db.Column(JSON)
    foreign_guid = db.Column(db.String)
    foreign_id = db.Column(db.Integer)
    url = db.Column(db.String(255))

    def __init__(self, site_id, title, date, duration, image, performers, status, local_path, year, episode_number, slug, overview, credits, release_date_utc, images, trailer, genres, foreign_guid, foreign_id, url):
        self.site_id = site_id
        self.title = title
        self.date = date
        self.duration = duration
        self.image = image
        self.performers = performers
        self.status = status
        self.local_path = local_path
        self.year = year
        self.episode_number = episode_number
        self.slug = slug
        self.overview = overview
        self.credits = credits
        self.release_date_utc = release_date_utc
        self.images = images
        self.trailer = trailer
        self.genres = genres
        self.foreign_guid = foreign_guid
        self.foreign_id = foreign_id
        self.url = url

    def to_dict(self):
        return {
            'id': self.id,
            'site_id': self.site_id,
            'title': self.title,
            'date': self.date,
            'duration': self.duration,
            'image': self.image,
            'performers': self.performers,
            'status': self.status,
            'local_path': self.local_path,
            'year': self.year,
            'episode_number': self.episode_number,
            'slug': self.slug,
            'overview': self.overview,
            'credits': self.credits,
            'release_date_utc': self.release_date_utc,
            'images': self.images,
            'trailer': self.trailer,
            'genres': self.genres,
            'foreign_guid': self.foreign_guid,
            'foreign_id': self.foreign_id,
            'url': self.url,
        }


class LibraryDirectory(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    path = db.Column(db.String, nullable=False, unique=True)


# Configure mappers to use confirm_deleted_rows=False
configure_mappers()
for mapper in db.Model.registry.mappers:
    mapper.confirm_deleted_rows = False

# Add indexing for the Scene model
Index('idx_scene_foreign_guid', Scene.foreign_guid)


class Log(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    level = db.Column(db.String, nullable=False)
    message = db.Column(db.Text, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.datetime.now(datetime.timezone.utc), nullable=False)
