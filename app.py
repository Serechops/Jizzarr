import asyncio
import datetime
import json
import logging
import mimetypes
import os
import queue
import re
import sys
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from watcher import main as watcher_main

import aiohttp
import ffmpeg
import pystray
import requests
from PIL import Image
from flask import Flask, request, jsonify, stream_with_context, render_template, Response, render_template_string, send_from_directory
from flask_cors import CORS
from fuzzywuzzy import fuzz
from mutagen.mp4 import MP4
from sqlalchemy import text, event, case, func
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from config import Config as AppConfig
from models import db, Config, Site, Scene, Log, LibraryDirectory

# Initialize the Flask application
app = Flask(__name__, instance_path=os.path.join(os.getcwd(), 'instance'))

# Ensure the instance folder exists
if not os.path.exists(app.instance_path):
    os.makedirs(app.instance_path)
# Use the instance folder for SQLite database
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(app.instance_path, 'jizzarr.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db.init_app(app)
event_queue = queue.Queue()
app.config.from_object(AppConfig)

# Create tables before the first request
with app.app_context():
    db.create_all()


class SSEHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.listeners = []

    def emit(self, record):
        log_entry = self.format(record)
        for listener in self.listeners:
            try:
                listener(log_entry)
            except Exception:
                self.listeners.remove(listener)

    def add_listener(self, listener):
        self.listeners.append(listener)


sse_handler = SSEHandler()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger()
logger.addHandler(sse_handler)


def push_event(event):
    event_queue.put(event)


def sse_log_stream():
    def stream():
        messages = []

        def new_message(msg):
            messages.append(msg)

        sse_handler.add_listener(new_message)

        while True:
            while messages:
                msg = messages.pop(0)
                yield f'data: {msg}\n\n'
            time.sleep(0.1)

    return Response(stream_with_context(stream()), content_type='text/event-stream')


@contextmanager
def session_scope():
    """Provide a transactional scope around a series of operations."""
    session = db.session
    try:
        yield session
        session.commit()
    except SQLAlchemyError as e:
        session.rollback()
        logger.error(f"Session rollback because of exception: {e}")
        raise
    finally:
        session.close()


@app.route('/log_stream')
def log_stream_route():
    return sse_log_stream()


def start_sse_thread():
    thread = threading.Thread(target=sse_log_stream)
    thread.daemon = True
    thread.start()


# Enable CORS for the app
CORS(app)


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/statistics')
def stats_page():
    return render_template('stats.html')


def get_config_value(key):
    config = Config.query.filter_by(key=key).first()
    value = config.value if config else ''
    app.logger.debug(f"Config value for {key}: {value}")
    return value


@app.route('/config_page', methods=['GET'])
def config_page():
    stash_endpoint = get_config_value('stashEndpoint')
    stash_api_key = get_config_value('stashApiKey')
    tpdb_api_key = get_config_value('tpdbApiKey')
    download_folder = get_config_value('downloadFolder')

    app.logger.debug(f"Fetched config values: stash_endpoint={stash_endpoint}, stash_api_key={stash_api_key}, tpdb_api_key={tpdb_api_key}, download_folder={download_folder}")

    library_directories = LibraryDirectory.query.all()
    sites = Site.query.all()

    libraries_with_sites = {}
    for library in library_directories:
        libraries_with_sites[library] = [site for site in sites if site.home_directory and site.home_directory.startswith(library.path)]

    return render_template('config.html', stash_endpoint=stash_endpoint, stash_api_key=stash_api_key, tpdb_api_key=tpdb_api_key, download_folder=download_folder, libraries_with_sites=libraries_with_sites)


@app.route('/save_config', methods=['POST'])
def save_config():
    try:
        config_data = request.json
        for key, value in config_data.items():
            # Ensure consistent key naming
            config = Config.query.filter_by(key=key).first()
            if config:
                config.value = value
            else:
                config = Config(key=key, value=value)
                db.session.add(config)
        db.session.commit()
        app.logger.debug(f"Saved config values: {config_data}")
        return jsonify({"message": "Configuration saved successfully"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/get_tpdb_api_key', methods=['GET'])
def get_tpdb_api_key():
    try:
        tpdb_api_key = Config.query.filter_by(key='tpdbApiKey').first()
        if tpdb_api_key:
            return jsonify({'tpdbApiKey': tpdb_api_key.value})
        else:
            log_entry('ERROR', 'TPDB API Key not found')
            return jsonify({'error': 'TPDB API Key not found'}), 404
    except Exception as e:
        log_entry('ERROR', f"Error retrieving TPDB API Key: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/collection')
def collection():
    return render_template('collection.html')


@app.route('/collection_data', methods=['GET'])
def collection_data():
    try:
        # Fetch all sites from the database
        all_sites = Site.query.order_by(Site.id).all()
        site_ids = [site.id for site in all_sites]

        # Perform a single query to get the counts
        scene_counts = db.session.query(
            Scene.site_id,
            func.count(Scene.id).label('total_scenes'),
            func.sum(case((Scene.status == 'Found', 1), else_=0)).label('found_scenes')
        ).filter(Scene.site_id.in_(site_ids)).group_by(Scene.site_id).all()

        # Organize counts by site_id
        counts_by_site = {count.site_id: {'total_scenes': count.total_scenes, 'found_scenes': count.found_scenes} for count in scene_counts}

        collection_data = []
        for site in all_sites:
            total_scenes = counts_by_site.get(site.id, {}).get('total_scenes', 0)
            found_scenes = counts_by_site.get(site.id, {}).get('found_scenes', 0)
            collection_data.append({
                'site': {
                    'uuid': site.uuid,
                    'name': site.name,
                    'url': site.url,
                    'description': site.description,
                    'rating': site.rating,
                    'network': site.network,
                    'parent': site.parent,
                    'logo': site.logo,
                    'home_directory': site.home_directory
                },
                'total_scenes': total_scenes,
                'collected_scenes': found_scenes
            })

        response = {
            'collection_data': collection_data,
            'total_sites': len(all_sites)
        }

        logger.info('Collection data retrieved successfully')
        return jsonify(response)
    except Exception as e:
        logger.error(f"Error retrieving collection data: {e}")
        return jsonify({"error": str(e)}), 500


def paginate_query(query, page, per_page):
    return query.limit(per_page).offset((page - 1) * per_page)


@app.route('/site_scenes/<site_uuid>', methods=['GET'])
def site_scenes(site_uuid):
    try:
        # Fetch site details from the database
        site = Site.query.filter_by(uuid=site_uuid).first()
        if not site:
            return jsonify({"error": "Site not found"}), 404

        # Get pagination parameters
        page = request.args.get('page', 1, type=int)
        per_page = request.args.get('per_page', 100, type=int)

        # Fetch scenes with pagination
        scenes_query = Scene.query.filter_by(site_id=site.id)
        total_scenes = scenes_query.count()
        scenes = paginate_query(scenes_query, page, per_page).all()

        scenes_data = []
        for scene in scenes:
            scenes_data.append({
                'id': scene.id,
                'title': scene.title,
                'date': scene.date,
                'duration': scene.duration,
                'image': scene.image,
                'performers': scene.performers,
                'status': scene.status,
                'local_path': scene.local_path,
                'year': scene.year,
                'episode_number': scene.episode_number,
                'slug': scene.slug,
                'overview': scene.overview,
                'credits': scene.credits,
                'release_date_utc': scene.release_date_utc,
                'images': scene.images,
                'trailer': scene.trailer,
                'genres': scene.genres,
                'foreign_guid': scene.foreign_guid,
                'foreign_id': scene.foreign_id,
                'url': scene.url
            })

        response = {
            'site': {
                'uuid': site.uuid,
                'name': site.name,
                'url': site.url,
                'description': site.description,
                'rating': site.rating,
                'network': site.network,
                'parent': site.parent,
                'logo': site.logo,
                'home_directory': site.home_directory
            },
            'scenes': scenes_data,
            'pagination': {
                'page': page,
                'per_page': per_page,
                'total_scenes': total_scenes
            }
        }

        logger.info('Site scenes retrieved successfully')
        return jsonify(response)
    except Exception as e:
        logger.error(f"Error retrieving site scenes: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/scenes_for_site/<site_uuid>', methods=['GET'])
def scenes_for_site(site_uuid):
    try:
        # Get pagination parameters
        page = request.args.get('page', 1, type=int)
        per_page = request.args.get('per_page', 100, type=int)

        scenes_query = Scene.query.filter_by(site_id=site_uuid)
        total_scenes = scenes_query.count()
        scenes = paginate_query(scenes_query, page, per_page).all()

        scene_data = []
        for scene in scenes:
            scene_data.append({
                'id': scene.id,
                'title': scene.title,
                'date': scene.date,
                'duration': scene.duration,
                'performers': scene.performers,
                'status': scene.status,
                'local_path': scene.local_path,
                'foreign_guid': scene.foreign_guid,
                'url': scene.url,
                'image': scene.image,
                'year': scene.year,
                'episode_number': scene.episode_number,
                'slug': scene.slug,
                'overview': scene.overview,
                'credits': scene.credits,
                'release_date_utc': scene.release_date_utc,
                'images': scene.images,
                'trailer': scene.trailer,
                'genres': scene.genres,
                'foreign_id': scene.foreign_id
            })

        logger.info(f'Scenes for site {site_uuid} retrieved successfully')
        return jsonify({
            'scenes': scene_data,
            'pagination': {
                'page': page,
                'per_page': per_page,
                'total_scenes': total_scenes
            }
        })
    except Exception as e:
        logger.error(f"Error retrieving scenes for site {site_uuid}: {e}")
        return jsonify({"error": str(e)}), 500


async def fetch_scenes(session, url, headers, site_uuid, page_scene, per_page_scene):
    try:
        async with session.get(url, headers=headers) as response:
            if response.status != 200:
                app.logger.error(f'Failed to fetch scenes for site UUID: {site_uuid} - {response.status}')
                return []
            response_json = await response.json()
            fetched_scenes = response_json.get('data', [])
            app.logger.info(f'Fetched {len(fetched_scenes)} scenes on page {page_scene} for site UUID: {site_uuid}')
            return fetched_scenes
    except Exception as e:
        app.logger.error(f'Error fetching scenes for site UUID: {site_uuid} on page {page_scene}: {e}')
        return []


async def fetch_all_scenes(headers, site_uuid, per_page_scene):
    scenes = []
    page_scene = 1
    async with aiohttp.ClientSession() as session:
        while True:
            url = f'https://api.theporndb.net/sites/{site_uuid}/scenes?page={page_scene}&per_page={per_page_scene}'
            fetched_scenes = await fetch_scenes(session, url, headers, site_uuid, page_scene, per_page_scene)
            if not fetched_scenes:
                break
            scenes.extend(fetched_scenes)
            page_scene += 1
    return scenes


def sync_fetch_all_scenes(headers, site_uuid, per_page_scene):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    return loop.run_until_complete(fetch_all_scenes(headers, site_uuid, per_page_scene))


@app.route('/add_site', methods=['POST'])
def add_site():
    data = request.json
    site_uuid = data['site']['uuid']

    rating = data['site']['rating']
    if rating == '':
        rating = None
    else:
        try:
            rating = float(rating)
        except (ValueError, TypeError):
            rating = None

    tpdb_api_key = Config.query.filter_by(key='tpdbApiKey').first()
    if not tpdb_api_key or not tpdb_api_key.value:
        return jsonify({'error': 'TPDB API Key not configured'}), 500

    headers = {'Authorization': f'Bearer {tpdb_api_key.value}'}

    try:
        existing_site = Site.query.filter_by(uuid=site_uuid).first()

        if existing_site:
            existing_site.name = data['site']['name']
            existing_site.url = data['site']['url']
            existing_site.description = data['site']['description']
            existing_site.rating = rating
            existing_site.network = data['site']['network']
            existing_site.parent = data['site']['parent']
            existing_site.logo = data['site'].get('logo', '')

            Scene.query.filter_by(site_id=existing_site.id).delete()
            scenes = []
            per_page_scene = 250

            fetched_scenes = sync_fetch_all_scenes(headers, site_uuid, per_page_scene)

            for scene_data in fetched_scenes:
                if not scene_data:
                    app.logger.error(f"Found NoneType entry in scenes list for site UUID: {site_uuid}")
                    continue

                performers = ', '.join([performer['name'] for performer in scene_data.get('performers', [])])
                existing_scene = Scene.query.filter_by(foreign_guid=scene_data.get('id')).first()

                if existing_scene:
                    app.logger.info(f"Scene with ForeignGuid {scene_data['id']} already exists. Skipping.")
                    continue

                new_scene = Scene(
                    site_id=existing_site.id,
                    title=scene_data.get('title'),
                    date=scene_data.get('date'),
                    duration=scene_data.get('duration'),
                    image=scene_data.get('image', ''),
                    performers=performers,
                    status=scene_data.get('status', ''),
                    local_path=scene_data.get('local_path', ''),
                    year=scene_data.get('year', 0),
                    episode_number=scene_data.get('episode_number', 0),
                    slug=scene_data.get('slug', ''),
                    overview=scene_data.get('overview', ''),
                    credits=json.dumps(scene_data.get('credits', [])),
                    release_date_utc=scene_data.get('release_date_utc', ''),
                    images=json.dumps(scene_data.get('images', [])),
                    trailer=scene_data.get('trailer', ''),
                    genres=json.dumps(scene_data.get('genres', [])),
                    foreign_guid=scene_data.get('id', ''),
                    foreign_id=scene_data.get('foreign_id', 0),
                    url=scene_data.get('url')
                )
                scenes.append(new_scene)

            db.session.bulk_save_objects(scenes)
            db.session.commit()
            log_entry('INFO', f'Site and scenes updated successfully for site UUID: {site_uuid}')
            return jsonify({'message': 'Site and scenes updated successfully!'}), 200
        else:
            site = Site(
                uuid=site_uuid,
                name=data['site']['name'],
                url=data['site']['url'],
                description=data['site']['description'],
                rating=rating,
                network=data['site']['network'],
                parent=data['site']['parent'],
                logo=data['site'].get('logo', '')
            )
            db.session.add(site)
            db.session.flush()  # Ensure site is committed before adding scenes

            scenes = []
            per_page_scene = 250

            fetched_scenes = sync_fetch_all_scenes(headers, site_uuid, per_page_scene)

            for scene_data in fetched_scenes:
                if not scene_data:
                    app.logger.error(f"Found NoneType entry in scenes list for site UUID: {site_uuid}")
                    continue

                performers = ', '.join([performer['name'] for performer in scene_data.get('performers', [])])
                existing_scene = Scene.query.filter_by(foreign_guid=scene_data.get('id')).first()

                if existing_scene:
                    app.logger.info(f"Scene with ForeignGuid {scene_data['id']} already exists. Skipping.")
                    continue

                new_scene = Scene(
                    site_id=site.id,
                    title=scene_data.get('title'),
                    date=scene_data.get('date'),
                    duration=scene_data.get('duration'),
                    image=scene_data.get('image', ''),
                    performers=performers,
                    status=scene_data.get('status', ''),
                    local_path=scene_data.get('local_path', ''),
                    year=scene_data.get('year', 0),
                    episode_number=scene_data.get('episode_number', 0),
                    slug=scene_data.get('slug', ''),
                    overview=scene_data.get('overview', ''),
                    credits=json.dumps(scene_data.get('credits', [])),
                    release_date_utc=scene_data.get('release_date_utc', ''),
                    images=json.dumps(scene_data.get('images', [])),
                    trailer=scene_data.get('trailer', ''),
                    genres=json.dumps(scene_data.get('genres', [])),
                    foreign_guid=scene_data.get('id', ''),
                    foreign_id=scene_data.get('foreign_id', 0),
                    url=scene_data.get('url')
                )
                scenes.append(new_scene)

            db.session.bulk_save_objects(scenes)
            db.session.commit()
            log_entry('INFO', f'Site and scenes added successfully for site UUID: {site_uuid}')
            return jsonify({'message': 'Site and scenes added successfully!'}), 201
    except Exception as e:
        log_entry('ERROR', f"Error adding site: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/remove_site/<string:site_uuid>', methods=['DELETE'])
def remove_site(site_uuid):
    try:
        site = Site.query.filter_by(uuid=site_uuid).first()
        if not site:
            log_entry('ERROR', f'Site not found for UUID: {site_uuid}')
            return jsonify({'error': 'Site not found'}), 404

        scenes = Scene.query.filter_by(site_id=site.id).all()
        scene_count = len(scenes)

        total_size_before = db.session.execute(text("PRAGMA page_count")).fetchone()[0] * db.session.execute(text("PRAGMA page_size")).fetchone()[0]

        for scene in scenes:
            db.session.delete(scene)
        db.session.commit()

        db.session.delete(site)
        db.session.commit()

        total_size_after = db.session.execute(text("PRAGMA page_count")).fetchone()[0] * db.session.execute(text("PRAGMA page_size")).fetchone()[0]
        space_saved = total_size_before - total_size_after

        log_entry('INFO', f'Removed site: {site.name} with UUID: {site_uuid}')
        log_entry('INFO', f'Removed {scene_count} scenes associated with the site')
        log_entry('INFO', f'Space saved: {space_saved} bytes')

        return jsonify({'message': 'Site and scenes removed successfully!', 'space_saved': space_saved}), 200
    except Exception as e:
        log_entry('ERROR', f"Error removing site: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/remove_scene/<int:scene_id>', methods=['DELETE'])
def remove_scene(scene_id):
    try:
        scene = db.session.get(Scene, scene_id)
        if not scene:
            log_entry('ERROR', f'Scene not found for ID: {scene_id}')
            return jsonify({'error': 'Scene not found'}), 404

        db.session.delete(scene)
        db.session.commit()

        log_entry('INFO', f'Scene removed successfully for ID: {scene_id}')
        return jsonify({'message': 'Scene removed successfully!'})
    except Exception as e:
        log_entry('ERROR', f"Error removing scene: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/match_scene', methods=['POST'])
def match_scene():
    try:
        data = request.json
        scene_id = data.get('scene_id')
        file_path = data.get('file_path')
        prepend_home_directory = data.get('prepend_home_directory', False)

        scene = Scene.query.get(scene_id)
        if not scene:
            return jsonify({'error': 'Scene not found'}), 404

        if prepend_home_directory:
            site = Site.query.get(scene.site_id)
            if site and site.home_directory:
                file_path = f"{site.home_directory}/{file_path}"

        scene.local_path = file_path
        scene.status = 'Found'
        db.session.commit()

        log_entry('INFO', f"Scene {scene_id} matched successfully with path: {file_path}")
        return jsonify({'message': 'Scene matched successfully!'}), 200
    except Exception as e:
        log_entry('ERROR', f"Error matching scene: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/set_home_directory', methods=['POST'])
def set_home_directory():
    data = request.json
    site_uuid = data.get('site_uuid')
    directory = data.get('directory')

    site = Site.query.filter_by(uuid=site_uuid).first()
    if not site:
        log_entry('ERROR', f'Site not found for UUID: {site_uuid}')
        return jsonify({'error': 'Site not found'}), 404

    site.home_directory = directory
    db.session.commit()

    log_entry('INFO', f'Home directory set successfully for site UUID: {site_uuid}')
    return jsonify({'message': 'Home directory set successfully!'})


def get_file_duration(file_path):
    try:
        file_extension = os.path.splitext(file_path)[1].lower()
        if file_extension in ['.mp4', '.avi', '.mov', '.mkv', '.flv', '.wmv', '.webm']:
            probe = ffmpeg.probe(file_path)
            duration = float(probe['format']['duration'])
            return duration / 60  # convert seconds to minutes
        else:
            print(f"Unsupported file format for file {file_path}")
    except Exception as e:
        print(f"Error getting duration for file {file_path}: {e}")
    return None


def clean_string(input_string):
    if isinstance(input_string, list):
        return ' '.join([re.sub(r'[^\w\s]', '', performer).lower() for performer in input_string])
    return re.sub(r'[^\w\s]', '', input_string).lower()


def extract_date_from_filename(filename):
    date_patterns = [
        r'(\d{4}-\d{2}-\d{2})',  # YYYY-MM-DD
        r'(\d{2}-\d{2}-\d{4})',  # DD-MM-YYYY
        r'(\d{2}-\d{2}-\d{2})',  # DD-MM-YY
    ]
    for pattern in date_patterns:
        match = re.search(pattern, filename)
        if match:
            return match.group(1)
    return None


def get_potential_matches(scenes, filenames, tolerance=95):
    potential_matches = []
    for scene in scenes:
        for filename in filenames:
            clean_filename = clean_string(str(filename))
            clean_scene_title = clean_string(scene.title)
            if fuzz.partial_ratio(clean_filename, clean_scene_title) >= tolerance:
                match_data = {
                    'scene_id': scene.id,
                    'suggested_file': str(filename),
                    'suggested_file_title': Path(filename).stem,  # Use Path.stem to get filename without extension
                    'title_score': fuzz.partial_ratio(clean_filename, clean_scene_title),
                }
                if scene.date:
                    clean_scene_date = clean_string(scene.date)
                    filename_date = extract_date_from_filename(clean_filename)
                    if filename_date and clean_string(filename_date) == clean_scene_date:
                        match_data['suggested_file_date'] = filename_date
                        match_data['date_score'] = 100

                if scene.duration:
                    file_duration = get_file_duration(str(filename))
                    if file_duration and abs(file_duration - scene.duration) < 1:  # tolerance of 1 minute
                        match_data['suggested_file_duration'] = file_duration
                        match_data['duration_score'] = 100

                if scene.performers:
                    clean_scene_performers = clean_string(scene.performers)
                    if fuzz.partial_ratio(clean_filename, clean_scene_performers) >= tolerance:
                        match_data['suggested_file_performers'] = Path(filename).stem  # Use Path.stem to get filename without extension
                        match_data['performers_score'] = fuzz.partial_ratio(clean_filename, clean_scene_performers)
                potential_matches.append(match_data)
    return potential_matches


@app.route('/suggest_matches', methods=['POST'])
def suggest_matches():
    try:
        data = request.json
        site_uuid = data.get('site_uuid')
        tolerance = data.get('tolerance', 95)

        log_entry('INFO', f'Received site_uuid: {site_uuid}')

        site = Site.query.filter_by(uuid=site_uuid).first()
        if not site:
            log_entry('ERROR', f'Site not found for UUID: {site_uuid}')
            return jsonify({'error': 'Site not found'}), 404

        if not site.home_directory:
            log_entry('ERROR', f'Home directory not set for site UUID: {site_uuid}')
            return jsonify({'error': 'Home directory not set'}), 404

        log_entry('INFO', f'Site found: {site.name}, Home directory: {site.home_directory}')

        scenes = Scene.query.filter_by(site_id=site.id).all()

        home_directory = Path(site.home_directory)

        # First, check for files with custom UUID tags
        files_with_tags = scan_directory_for_files(home_directory)
        tagged_matches = []
        for file_path, uuid in files_with_tags:
            matching_scene = next((scene for scene in scenes if scene.foreign_guid == uuid), None)
            if matching_scene:
                tagged_matches.append({
                    'scene_id': matching_scene.id,
                    'suggested_file': str(file_path),
                    'suggested_file_title': Path(str(file_path)).stem,
                    'uuid': uuid,
                    'match_type': 'UUID'
                })

        # If no matches found via UUID, proceed with other matching methods
        if not tagged_matches:
            filenames = [f for f in home_directory.glob('**/*') if f.is_file() and mimetypes.guess_type(f)[0] and mimetypes.guess_type(f)[0].startswith('video/')]
            potential_matches = get_potential_matches(scenes, filenames, tolerance)
        else:
            potential_matches = tagged_matches

        log_entry('INFO', f'Potential matches suggested for site UUID: {site_uuid}')
        return jsonify(potential_matches)
    except Exception as e:
        log_entry('ERROR', f"Error suggesting matches: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/match_by_uuid', methods=['POST'])
def match_by_uuid():
    try:
        data = request.json
        site_uuid = data.get('site_uuid')

        site = Site.query.filter_by(uuid=site_uuid).first()
        if not site or not site.home_directory:
            log_entry('ERROR', f'Site or home directory not found for UUID: {site_uuid}')
            return jsonify({'error': 'Site or home directory not found'}), 404

        scenes = Scene.query.filter_by(site_id=site.id).all()

        home_directory = Path(site.home_directory)

        # Check for files with custom UUID tags
        log_entry('DEBUG', f'Start scanning directory for files with UUID tags: {home_directory}')
        files_with_tags = scan_directory_for_files(home_directory)
        log_entry('DEBUG', f'Files with UUID tags found: {files_with_tags}')

        tagged_matches = []
        for file_path, uuid in files_with_tags:
            log_entry('DEBUG', f'Checking file: {file_path} with UUID: {uuid}')
            matching_scene = next((scene for scene in scenes if scene.foreign_guid == uuid.decode('utf-8')), None)
            if matching_scene:
                log_entry('INFO', f'Match found: File {file_path} matches scene ID {matching_scene.id} with UUID {uuid}')
                tagged_matches.append({
                    'scene_id': matching_scene.id,
                    'suggested_file': str(file_path),
                    'suggested_file_title': Path(str(file_path)).stem,
                    'uuid': uuid.decode('utf-8'),
                    'match_type': 'UUID'
                })
            else:
                log_entry('DEBUG', f'No match found for file: {file_path} with UUID: {uuid}')

        log_entry('INFO', f'Tagged matches suggested for site UUID: {site_uuid}')
        return jsonify(tagged_matches)
    except Exception as e:
        log_entry('ERROR', f"Error suggesting matches by UUID: {e}")
        return jsonify({"error": str(e)}), 500


def fetch_custom_tag(file_path):
    try:
        video = MP4(file_path)
        if "----:com.apple.iTunes:UUID" in video:
            return video["----:com.apple.iTunes:UUID"][0]  # Retrieve the custom UUID tag
        else:
            return None
    except Exception as e:
        print(f"Failed to read metadata from {file_path}: {e}")
        return None


def scan_directory_for_files(directory):
    supported_extensions = ['.mp4', '.m4v', '.mov']
    files_with_tags = []

    for root, dirs, files in os.walk(directory):
        for file in files:
            if any(file.endswith(ext) for ext in supported_extensions):
                file_path = os.path.join(root, file)
                uuid = fetch_custom_tag(file_path)
                if uuid:
                    files_with_tags.append((file_path, uuid))

    return files_with_tags


def match_scene_by_uuid(uuid, file_path):
    with app.app_context():
        matching_scene = Scene.query.filter_by(foreign_guid=uuid).first()
        if matching_scene:
            matching_scene.local_path = file_path
            matching_scene.status = 'Found'
            db.session.commit()
            log_entry('INFO', f'Automatically matched scene ID: {matching_scene.id} with file: {file_path}')
            return True
        return False


@app.route('/search_stash_for_matches', methods=['POST'])
def search_stash_for_matches():
    try:
        data = request.json
        site_uuid = data.get('site_uuid')

        if not site_uuid:
            log_entry('ERROR', 'Site UUID is required for search_stash_for_matches')
            return jsonify({'error': 'Site UUID is required'}), 400

        site = Site.query.filter_by(uuid=site_uuid).first()
        if not site:
            log_entry('ERROR', f'Site not found for UUID: {site_uuid}')
            return jsonify({'error': 'Site not found'}), 404

        scenes = Scene.query.filter_by(site_id=site.id).all()
        stash_matches = []

        stash_endpoint = Config.query.filter_by(key='stashEndpoint').first()
        stash_api_key = Config.query.filter_by(key='stashApiKey').first()

        if not stash_endpoint or not stash_api_key:
            log_entry('ERROR', 'Stash endpoint or API key not configured')
            return jsonify({'error': 'Stash endpoint or API key not configured'}), 500

        local_endpoint = stash_endpoint.value
        local_headers = {
            "Accept-Encoding": "gzip, deflate, br",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "ApiKey": f"{stash_api_key.value}"
        }

        for scene in scenes:
            foreign_guid = scene.foreign_guid
            if not foreign_guid:
                continue

            query = {
                "query": f"""
                    query FindScenes {{
                        findScenes(
                            scene_filter: {{
                                stash_id_endpoint: {{
                                    stash_id: "{foreign_guid}"
                                    modifier: EQUALS
                                }}
                            }}
                        ) {{
                            scenes {{
                                id
                                title
                                files {{
                                    path
                                }}
                            }}
                        }}
                    }}
                """
            }

            try:
                response = requests.post(local_endpoint, json=query, headers=local_headers)
            except requests.exceptions.RequestException as e:
                log_entry('ERROR', f'Error fetching data from Stash: {e}')
                continue

            if response.status_code != 200:
                log_entry('ERROR', f'Failed to fetch data from Stash for scene ID: {scene.id}')
                continue

            result = response.json()
            matched_scenes = result['data']['findScenes']['scenes']

            if matched_scenes:
                matched_scene = matched_scenes[0]
                stash_matches.append({
                    'scene_id': scene.id,
                    'matched_scene_id': matched_scene['id'],
                    'matched_title': matched_scene['title'],
                    'matched_file_path': matched_scene['files'][0]['path'],
                    'foreign_guid': foreign_guid,
                    'scene_title': scene.title,
                    'scene_date': scene.date,
                    'scene_duration': scene.duration,
                    'scene_performers': scene.performers,
                    'scene_status': scene.status,
                    'scene_local_path': scene.local_path
                })

        log_entry('INFO', f'Stash matches searched successfully for site UUID: {site_uuid}')
        return jsonify(stash_matches)
    except Exception as e:
        log_entry('ERROR', f"Error searching stash for matches: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/get_site_uuid', methods=['POST'])
def get_site_uuid():
    data = request.json
    site_title = data.get('site_title')

    if not site_title:
        log_entry('ERROR', 'Site title is required for get_site_uuid')
        return jsonify({'error': 'Site title is required'}), 400

    site = Site.query.filter_by(name=site_title).first()
    if not site:
        log_entry('ERROR', f'Site not found for title: {site_title}')
        return jsonify({'error': 'Site not found'}), 404

    log_entry('INFO', f'Site UUID retrieved successfully for title: {site_title}')
    return jsonify({'site_uuid': site.uuid})


@app.route('/collection_stats', methods=['GET'])
def collection_stats():
    try:
        total_scenes = Scene.query.count()
        collected_scenes = Scene.query.filter(Scene.status == 'Found', Scene.local_path.isnot(None)).count()

        collected_duration = db.session.query(func.sum(Scene.duration)).filter(Scene.status == 'Found', Scene.local_path.isnot(None)).scalar() or 0
        total_duration = db.session.query(func.sum(Scene.duration)).scalar() or 0
        missing_duration = total_duration - collected_duration
        avg_rating = db.session.query(func.avg(Site.rating)).scalar() or 0

        stats = {
            'total': total_scenes,
            'collected': collected_scenes,
            'collected_duration': collected_duration,
            'missing_duration': missing_duration,
            'total_duration': total_duration,
            'avg_rating': round(avg_rating, 2)
        }

        log_entry('INFO', 'Collection stats retrieved successfully')
        return jsonify(stats)
    except Exception as e:
        log_entry('ERROR', f"Error retrieving collection stats: {e}")
        return jsonify({"error": str(e)}), 500


@app.before_request
def before_request():
    if 'sqlite' in app.config['SQLALCHEMY_DATABASE_URI']:
        db.session.execute(text('PRAGMA busy_timeout = 30000'))  # 30 seconds


progress = {"total": 0, "completed": 0}


@app.route('/progress')
def get_progress():
    def generate():
        while True:
            data = json.dumps(progress)
            yield f"data: {data}\n\n"
            time.sleep(1)

    return Response(generate(), mimetype='text/event-stream')


def populate_from_stash_thread():
    with app.app_context():
        try:
            log_entry('INFO', 'Starting populate_from_stash_thread')

            stash_endpoint = Config.query.filter_by(key='stashEndpoint').first()
            stash_api_key = Config.query.filter_by(key='stashApiKey').first()
            tpdb_api_key = Config.query.filter_by(key='tpdbApiKey').first()

            if not stash_endpoint or not stash_api_key or not tpdb_api_key:
                log_entry('ERROR', 'Stash endpoint, Stash API key, or TPDB API key not configured')
                return

            log_entry('INFO', 'Fetching scenes from Stash')
            query = """
            query FindScenes {
                findScenes(
                    scene_filter: {
                        stash_id_endpoint: {
                            endpoint: "https://theporndb.net/graphql"
                            modifier: INCLUDES
                        }
                    },
                    filter: { per_page: -1, direction: ASC }
                ) {
                    scenes {
                        studio {
                            name
                        }
                    }
                }
            }
            """

            response = requests.post(stash_endpoint.value, json={'query': query}, headers={
                "ApiKey": f"{stash_api_key.value}",
                "Content-Type": "application/json"
            })

            if response.status_code != 200:
                log_entry('ERROR', f'Failed to fetch data from Stash: {response.status_code} - {response.text}')
                return

            data = response.json()
            log_entry('DEBUG', f'Scenes data received from Stash: {json.dumps(data)}')
            scenes = data.get('data', {}).get('findScenes', {}).get('scenes', [])

            if not scenes:
                log_entry('INFO', 'No scenes found from Stash')
                return

            # Use a dictionary to collect studio names and their scene counts
            studio_scene_counts = {}
            for scene in scenes:
                studio = scene.get('studio')
                if studio:
                    studio_name = studio.get('name')
                    if studio_name:
                        if studio_name in studio_scene_counts:
                            studio_scene_counts[studio_name] += 1
                        else:
                            studio_scene_counts[studio_name] = 1

            # Sort studios by scene count in ascending order
            sorted_studios = sorted(studio_scene_counts.items(), key=lambda item: item[1])

            log_entry('DEBUG', f'Studios sorted by scene count: {sorted_studios}')
            progress['total'] = len(sorted_studios)
            progress['completed'] = 0

            headers = {'Authorization': f'Bearer {tpdb_api_key.value}'}

            # Process each studio, sorted by the number of scenes
            for studio_name, scene_count in sorted_studios:
                process_studio_and_add_site(studio_name, headers, app)

            delete_duplicate_scenes()

            log_entry('INFO', 'Sites and scenes populated from Stash')

            progress['total'] = 0
            progress['completed'] = 0
        except Exception as e:
            log_entry('ERROR', f"Error populating from stash: {e}")
            progress['total'] = 0
            progress['completed'] = 0



def process_studio_and_add_site(studio_name, headers, app):
    with app.app_context():
        log_entry('INFO', f'Starting to process studio: {studio_name}')
        search_url = f"https://api.theporndb.net/jizzarr/site/search?q={studio_name}"
        try:
            search_response = requests.get(search_url, headers=headers)
        except requests.RequestException as e:
            log_entry('WARNING', f'Error fetching data for studio {studio_name}: {e}')
            return

        if search_response.status_code != 200:
            log_entry('WARNING', f'Failed to fetch data for studio: {studio_name} - {search_response.status_code} - {search_response.text}')
            return

        search_results = search_response.json()
        log_entry('DEBUG', f'Search results for {studio_name}: {json.dumps(search_results)}')
        
        for site_data in search_results:
            log_entry('DEBUG', f'Processing site data: {json.dumps(site_data)}')

            # Prepare site data in the format expected by add_site
            site_payload = {
                'site': {
                    'uuid': site_data['ForeignGuid'],
                    'name': site_data['Title'],
                    'url': site_data['Homepage'],
                    'description': site_data['Overview'],
                    'rating': None,
                    'network': site_data['Network'],
                    'parent': '',
                    'logo': next((img['Url'] for img in site_data['Images'] if img['CoverType'] == 'Logo'), '')
                }
            }

            # Trigger the add_site function
            add_site_response = app.test_client().post('/add_site', json=site_payload)
            if add_site_response.status_code in (200, 201):
                log_entry('INFO', f'Successfully added/updated site: {site_data["Title"]}')
            else:
                log_entry('ERROR', f'Failed to add/update site: {site_data["Title"]}, Response: {add_site_response.json}')

        progress['completed'] += 1
        log_entry('INFO', f'Progress: {progress["completed"]}/{progress["total"]}')

@app.route('/populate_from_stash', methods=['POST'])
def populate_from_stash():
    stash_endpoint = Config.query.filter_by(key='stashEndpoint').first()
    stash_api_key = Config.query.filter_by(key='stashApiKey').first()
    tpdb_api_key = Config.query.filter_by(key='tpdbApiKey').first()

    if not stash_endpoint or not stash_api_key or not tpdb_api_key:
        return jsonify({'error': 'Stash endpoint, Stash API key, or TPDB API key not configured'}), 400

    thread = threading.Thread(target=populate_from_stash_thread)
    thread.start()
    return jsonify({'message': 'Stash population started'}), 202

def fetch_scenes_data(foreign_id, headers):
    url = f"https://api.theporndb.net/jizzarr/site/{foreign_id}"
    response = requests.get(url, headers=headers)
    if response.status_code == 200:
        site_data = response.json()
        return site_data.get('Episodes', [])
    return []


def delete_duplicate_scenes():
    subquery = db.session.query(Scene.foreign_guid, func.count(Scene.id).label('count')).group_by(Scene.foreign_guid).having(func.count(Scene.id) > 1).subquery()

    duplicates = db.session.query(Scene).join(subquery, Scene.foreign_guid == subquery.c.foreign_guid).all()

    total_size_saved = 0
    deleted_scenes = []

    seen_guids = set()

    for duplicate in duplicates:
        if duplicate.foreign_guid in seen_guids:
            try:
                scene_size = os.path.getsize(duplicate.local_path) if duplicate.local_path else 0
                total_size_saved += scene_size
                deleted_scenes.append({
                    'id': duplicate.id,
                    'title': duplicate.title,
                    'site_id': duplicate.site_id,
                    'foreign_guid': duplicate.foreign_guid,
                    'local_path': duplicate.local_path,
                    'size': scene_size
                })
                db.session.delete(duplicate)
            except Exception as e:
                log_entry('ERROR', f"Error deleting scene {duplicate.id}: {e}")
        else:
            seen_guids.add(duplicate.foreign_guid)

    db.session.commit()

    log_entry('INFO', f"Deleted {len(deleted_scenes)} duplicate scenes")
    for scene in deleted_scenes:
        log_entry('INFO', f"Deleted scene ID: {scene['id']}, Title: {scene['title']}, Site ID: {scene['site_id']}, Foreign GUID: {scene['foreign_guid']}, Local Path: {scene['local_path']}, Size: {scene['size']} bytes")

    total_size_mb = total_size_saved / (1024 * 1024)
    log_entry('INFO', f"Total space saved: {total_size_mb:.2f} MB")


def log_entry(level, message):
    log = Log(level=level, message=message)
    db.session.add(log)
    db.session.commit()


@app.route('/logs')
def logs():
    logs = Log.query.order_by(Log.timestamp.desc()).all()
    log_entries = []
    for log in logs:
        log_entries.append({
            'level': log.level,
            'message': log.message,
            'timestamp': log.timestamp
        })
    return render_template('logs.html', logs=log_entries)


LOGS_FOLDER = os.path.join(app.static_folder, 'logs')
os.makedirs(LOGS_FOLDER, exist_ok=True)


@app.route('/logs_data', methods=['GET'])
def logs_data():
    try:
        page = int(request.args.get('page', 1))
        per_page = int(request.args.get('per_page', 20))
        search = request.args.get('search', '').lower()

        logger.debug(f"Fetching logs for page {page} with {per_page} items per page and search term '{search}'")

        query = Log.query
        if search:
            query = query.filter(Log.message.ilike(f'%{search}%') | Log.level.ilike(f'%{search}%'))

        logs_paginate = query.paginate(page=page, per_page=per_page, error_out=False)
        logs = [{
            'timestamp': log.timestamp.strftime('%Y-%m-%d %H:%M:%S'),
            'level': log.level,
            'message': log.message
        } for log in logs_paginate.items]

        response = {
            'logs': logs,
            'total_pages': logs_paginate.pages,
            'current_page': logs_paginate.page
        }

        logger.info('Logs data retrieved successfully')
        return jsonify(response)
    except Exception as e:
        logger.error(f"Error retrieving logs data: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/download_logs')
def download_logs():
    timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    log_filename = f'logs_{timestamp}.txt'
    log_filepath = os.path.join(LOGS_FOLDER, log_filename)

    logs = Log.query.order_by(Log.timestamp).all()
    with open(log_filepath, 'w') as f:
        for log in logs:
            f.write(f"{log.timestamp.strftime('%Y-%m-%d %H:%M:%S')} - {log.level} - {log.message}\n")

    return send_from_directory(LOGS_FOLDER, log_filename, as_attachment=True)


@app.route('/clear_logs', methods=['POST'])
def clear_logs():
    try:
        # Export logs before clearing
        timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
        log_filename = f'logs_{timestamp}.txt'
        log_filepath = os.path.join(LOGS_FOLDER, log_filename)

        logs = Log.query.order_by(Log.timestamp).all()
        with open(log_filepath, 'w') as f:
            for log in logs:
                f.write(f"{log.timestamp.strftime('%Y-%m-%d %H:%M:%S')} - {log.level} - {log.message}\n")

        # Clear logs
        Log.query.delete()
        db.session.commit()

        logger.info('Logs cleared successfully')
        return jsonify({'success': True})
    except Exception as e:
        logger.error(f"Error clearing logs: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/get_metadata', methods=['POST'])
def get_metadata():
    try:
        data = request.json
        app.logger.debug(f"Incoming request data: {data}")

        if not data or 'scene_url' not in data:
            app.logger.error("No scene_url provided in request")
            return jsonify({'error': 'scene_url not provided'}), 400

        scene_url = data['scene_url']
        app.logger.debug(f"Looking up scene with URL: {scene_url}")
        scene = Scene.query.filter_by(url=scene_url).first()
        if not scene:
            app.logger.error(f"Scene not found for URL: {scene_url}")
            return jsonify({'error': 'Scene not found'}), 404

        app.logger.debug(f"Scene found: {scene.title} (ID: {scene.id})")
        site = Site.query.get(scene.site_id)
        if not site:
            app.logger.error(f"Site not found for ID: {scene.site_id}")
            return jsonify({'error': 'Site not found'}), 404

        app.logger.debug(f"Site found: {site.name} (ID: {site.id})")
        performers = [{'name': performer.strip()} for performer in scene.performers.split(',')]

        metadata = {
            'site': {'name': site.name},
            'title': scene.title,
            'performers': performers,
            'date': scene.date,
            'foreign_guid': scene.foreign_guid,
            'extension': '.mp4'
        }

        app.logger.debug(f"Returning metadata: {metadata}")
        return jsonify(metadata)
    except Exception as e:
        app.logger.error(f"An error occurred: {e}")
        return jsonify({'error': 'Internal Server Error'}), 500


def fetch_metadata_from_file(file_path):
    try:
        video = MP4(file_path)
        metadata = {}
        if "----:com.apple.iTunes:UUID" in video:
            metadata['foreign_guid'] = video["----:com.apple.iTunes:UUID"][0].decode('utf-8')
        return metadata
    except Exception as e:
        logger.error(f"Failed to fetch metadata from {file_path}: {e}")
        return None


# Global variable to keep track of the process state
__stop_populate_sites = False


@app.route('/stop_populate_sites', methods=['POST'])
def stop_populate_sites():
    global __stop_populate_sites
    __stop_populate_sites = True
    return jsonify({'message': 'Populate Sites process stopped'}), 200


@app.route('/set_library_directory', methods=['POST'])
def set_library_directory():
    data = request.json
    library_directory = data.get('libraryDirectory')

    if not library_directory:
        return jsonify({'error': 'Library directory is required'}), 400

    try:
        new_directory = LibraryDirectory(path=library_directory)
        db.session.add(new_directory)
        db.session.commit()

        matches = scan_and_match_directories(library_directory)

        return jsonify({'message': 'Library directory set successfully', 'matches': matches}), 200
    except Exception as e:
        logger.error(f"Error setting library directory: {e}")
        return jsonify({'error': 'Failed to set library directory'}), 500


def scan_and_match_directories(base_path):
    try:
        subdirectories = [f.path for f in os.scandir(base_path) if f.is_dir()]
        sites = Site.query.all()
        site_names = {site.name.lower(): site for site in sites}

        matches = []
        logger.info(f"Scanning subdirectories in: {base_path}")
        for subdir in subdirectories:
            subdir_name = os.path.basename(subdir).lower()
            logger.info(f"Found subdirectory: {subdir_name}")
            if subdir_name in site_names:
                site = site_names[subdir_name]
                site.home_directory = subdir
                matches.append({'site_name': site.name, 'directory': subdir})
                logger.info(f"Matched site '{site.name}' to directory '{subdir}'")
                db.session.commit()

        return matches
    except Exception as e:
        logger.error(f"Error scanning and matching directories: {str(e)}")
        return str(e)


@app.route('/remove_library/<int:library_id>', methods=['DELETE'])
def remove_library(library_id):
    try:
        library = LibraryDirectory.query.get(library_id)
        if not library:
            return jsonify({'error': 'Library not found'}), 404

        db.session.delete(library)
        db.session.commit()

        return jsonify({'message': 'Library removed successfully'}), 200
    except Exception as e:
        logger.error(f"Error removing library: {e}")
        return jsonify({'error': 'Failed to remove library'}), 500


@app.route('/scan_libraries', methods=['POST'])
def scan_libraries():
    try:
        libraries = LibraryDirectory.query.all()
        matches = []

        for library in libraries:
            home_directory = Path(library.path)
            if home_directory.is_dir():
                for site in Site.query.all():
                    site_directory = home_directory / site.name
                    if site_directory.is_dir():
                        site.home_directory = str(site_directory)
                        db.session.commit()
                        matches.append({'site_name': site.name, 'directory': str(site_directory)})

        log_entry('INFO', 'Manual scan for library changes completed successfully')
        return jsonify({'message': 'Scan completed successfully!', 'matches': matches}), 200
    except Exception as e:
        log_entry('ERROR', f"Error during manual scan for library changes: {e}")
        return jsonify({'error': str(e)}), 500


# Set up logging to capture log records in a list
log_records = []


class ListHandler(logging.Handler):
    def emit(self, record):
        log_records.append(self.format(record))


list_handler = ListHandler()
logging.getLogger().addHandler(list_handler)


# Log all SQL statements
@event.listens_for(Engine, "before_cursor_execute")
def before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
    logger.info(f"Start Query: {statement}")
    logger.info(f"Parameters: {parameters}")


@event.listens_for(Engine, "after_cursor_execute")
def after_cursor_execute(conn, cursor, statement, parameters, context, executemany):
    logger.info(f"End Query: {statement}")


# Log commits
@event.listens_for(Engine, "commit")
def commit(conn):
    logger.info("Commit")


# Log rollbacks
@event.listens_for(Engine, "rollback")
def rollback(conn):
    logger.info("Rollback")


@app.route('/db_logs')
def db_logs():
    return render_template_string("""
        <html>
            <head>
                <title>Database Logs</title>
            </head>
            <body>
                <h1>Database Logs</h1>
                <pre>{{ logs }}</pre>
            </body>
        </html>
    """, logs="\n".join(log_records))


@app.route('/scenes_data', methods=['GET'])
def scenes_data():
    try:
        site_uuid = request.args.get('site_uuid')
        site = Site.query.filter_by(uuid=site_uuid).first()
        if not site:
            raise ValueError('Site not found')

        scenes = Scene.query.filter_by(site_id=site.id).all()
        scenes_data = [scene.to_dict() for scene in scenes]

        return jsonify({'scenes': scenes_data})
    except Exception as e:
        logger.error(f"Error retrieving scenes data: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/search_stash_for_all_sites', methods=['POST'])
def search_stash_for_all_sites():
    try:
        # Fetch config values
        stash_endpoint = Config.query.filter_by(key='stashEndpoint').first()
        stash_api_key = Config.query.filter_by(key='stashApiKey').first()

        app.logger.debug(f"Stash Endpoint: {stash_endpoint}")
        app.logger.debug(f"Stash API Key: {stash_api_key}")

        # Check if both stash_endpoint and stash_api_key are configured
        if not stash_endpoint or not stash_api_key:
            log_entry('ERROR', 'Stash endpoint or API key not configured')
            return jsonify({'error': 'Stash endpoint or API key not configured'}), 500

        # Prepare headers and endpoint for the API request
        local_endpoint = stash_endpoint.value
        local_headers = {
            "Accept-Encoding": "gzip, deflate, br",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "ApiKey": f"{stash_api_key.value}"
        }

        app.logger.debug(f"Local Endpoint: {local_endpoint}")
        app.logger.debug(f"Headers: {local_headers}")

        # GraphQL query to find all scenes with the specified endpoint
        query = """
            query FindScenes {
                findScenes(
                    scene_filter: {
                        stash_id_endpoint: {
                            endpoint: "https://theporndb.net/graphql"
                            modifier: INCLUDES
                        }
                    }
                    filter: { per_page: -1, direction: ASC }
                ) {
                    scenes {
                        files {
                            path
                        }
                        stash_ids {
                            endpoint
                            stash_id
                        }
                    }
                }
            }
        """

        app.logger.debug(f"GraphQL Query: {query}")

        # Make the API request
        response = requests.post(local_endpoint, json={"query": query}, headers=local_headers)

        # Log the status code and response content
        app.logger.debug(f"Response Status Code: {response.status_code}")
        app.logger.debug(f"Response Content: {response.content.decode('utf-8')}")

        # Check if the response is successful
        if response.status_code != 200:
            log_entry('ERROR', f"Failed to fetch data from Stash: {response.status_code} - {response.text}")
            return jsonify({"error": f"Failed to fetch data from Stash: {response.status_code}"}), response.status_code

        # Parse the JSON response
        result = response.json()

        # Validate the structure of the response
        if not result or 'data' not in result or 'findScenes' not in result['data']:
            app.logger.debug(f"Unexpected Result Structure: {result}")
            log_entry('ERROR', 'Invalid or unexpected response structure from Stash')
            return jsonify({"error": "Invalid response structure from Stash"}), 500

        # Extract scenes from the response
        stash_scenes = result.get('data', {}).get('findScenes', {}).get('scenes', [])

        app.logger.debug(f"Number of Scenes Retrieved from Stash: {len(stash_scenes)}")

        # If no scenes are found, log the information and return
        if not stash_scenes:
            log_entry('INFO', 'No scenes found in Stash with the specified endpoint')
            return jsonify({'message': 'No scenes found in Stash with the specified endpoint'}), 200

        all_matches = []

        # Iterate over each scene retrieved from Stash
        for stash_scene in stash_scenes:
            stash_ids = stash_scene.get('stash_ids', [])
            files = stash_scene.get('files', [])

            app.logger.debug(f"Processing Scene: {stash_scene}")
            if not files:
                app.logger.debug("Skipping scene due to missing files.")
                continue  # Skip if there are no files associated with this scene

            for stash_id_info in stash_ids:
                if stash_id_info.get('endpoint') == "https://theporndb.net/graphql":
                    stash_id = stash_id_info.get('stash_id')
                    file_path = files[0].get('path')

                    app.logger.debug(f"Stash ID: {stash_id}, File Path: {file_path}")

                    if not stash_id or not file_path:
                        app.logger.debug("Skipping due to missing stash_id or file_path.")
                        continue

                    # Look for a matching scene in the local database by foreign_guid
                    local_scene = Scene.query.filter_by(foreign_guid=stash_id).first()
                    app.logger.debug(f"Matching Local Scene: {local_scene}")

                    if local_scene:
                        # Update the local scene with the file path
                        local_scene.local_path = file_path
                        local_scene.status = 'Found'
                        db.session.commit()

                        # Add the match to the results
                        all_matches.append({
                            'scene_id': local_scene.id,
                            'site_id': local_scene.site_id,
                            'site_name': local_scene.site.name,
                            'matched_scene_id': stash_id,
                            'matched_title': local_scene.title,
                            'matched_file_path': file_path,
                            'foreign_guid': stash_id,
                            'scene_title': local_scene.title,
                            'scene_date': local_scene.date,
                            'scene_duration': local_scene.duration,
                            'scene_performers': local_scene.performers,
                            'scene_status': local_scene.status,
                            'scene_local_path': local_scene.local_path
                        })

        log_entry('INFO', 'Stash matches searched and updated successfully for all sites')
        return jsonify(all_matches)

    except Exception as e:
        app.logger.error(f"Exception occurred: {e}", exc_info=True)
        log_entry('ERROR', f"Error searching stash for matches across all sites: {e}")
        return jsonify({"error": str(e)}), 500


def main():
    def on_clicked(icon, item):
        if item.text == "Open Jizzarr":
            webbrowser.open("http://127.0.0.1:6900")
        elif item.text == "Quit":
            icon.stop()
            sys.exit(0)

    def run_tray_icon():
        icon_path = os.path.join(app.root_path, 'static', 'favicon.ico')
        icon = pystray.Icon("Jizzarr")
        icon.icon = Image.open(icon_path)
        icon.title = "Jizzarr"
        icon.menu = pystray.Menu(
            pystray.MenuItem("Open Jizzarr", on_clicked),
            pystray.MenuItem("Quit", on_clicked)
        )
        icon.run()

    def run_flask():
        with app.app_context():
            db.create_all()
            delete_duplicate_scenes()
        app.run(debug=False, host='0.0.0.0', port=6900)

    # Start the Flask app in a separate thread
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()

    # Open the browser tab
    webbrowser.open_new('http://127.0.0.1:6900')
    watcher_thread = threading.Thread(target=watcher_main)
    watcher_thread.daemon = True
    watcher_thread.start()

    # Run the system tray icon
    run_tray_icon()


if __name__ == '__main__':
    main()
