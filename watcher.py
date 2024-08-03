import json
import logging
import os
import re
import shutil
import time
from pathlib import Path

import requests
from flask import Flask
from mutagen.mp4 import MP4, MP4Tags
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from models import db, Site, Scene, Config

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Flask application for database context
app = Flask(__name__)
# Using an absolute path to the database file
db_path = os.path.abspath(os.path.join(os.path.dirname(__file__), 'instance', 'jizzarr.db'))
app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{db_path}'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db.init_app(app)


def initialize_database():
    with app.app_context():
        db.create_all()
        # Log the existing tables
        inspector = inspect(db.engine)
        tables = inspector.get_table_names()
        if 'site' not in tables:
            logger.error("The 'site' table does not exist. Ensure database initialization.")
            raise Exception("Database not properly initialized. 'site' table is missing.")


class Watcher:
    DIRECTORY_TO_WATCH = None

    def __init__(self):
        self.observer = Observer()

    def run(self):
        if not self.DIRECTORY_TO_WATCH:
            logger.error("No directory to watch")
            return
        self.scan_existing_files(self.DIRECTORY_TO_WATCH)  # Scan existing files on start
        event_handler = Handler()
        self.observer.schedule(event_handler, self.DIRECTORY_TO_WATCH, recursive=True)
        self.observer.start()
        logger.info(f"Started watching directory: {self.DIRECTORY_TO_WATCH}")
        try:
            while True:
                time.sleep(5)
        except KeyboardInterrupt:
            self.observer.stop()
            logger.info("Observer stopped")
        self.observer.join()

    @staticmethod
    def scan_existing_files(directory):
        logger.info(f"Scanning existing files in directory: {directory}")
        for root, _, files in os.walk(directory):
            for file in files:
                file_path = os.path.join(root, file)
                if file_path.endswith(('.mp4', '.m4v', '.mov', '.json')):
                    logger.info(f"Processing existing file: {file_path}")
                    Handler().process_file(file_path, directory)


class Handler(FileSystemEventHandler):
    def process(self, event):
        if event.is_directory:
            return None
        elif event.event_type == 'created':
            logger.info(f"New file detected: {event.src_path}")

            # Ignore .part files
            if event.src_path.endswith('.part'):
                logger.info(f"Ignoring .part file: {event.src_path}")
                return

            # Wait for the file to be completely written
            time.sleep(5)  # Adjust the delay as necessary

            # Ensure the file exists and is not empty
            if not os.path.exists(event.src_path) or os.path.getsize(event.src_path) == 0:
                logger.warning(f"File {event.src_path} does not exist or is empty after waiting")
                return

            if event.src_path.endswith(('.mp4', '.m4v', '.mov', '.json')):
                logger.info(f"Detected new file: {event.src_path}")
                self.process_file(event.src_path, Watcher.DIRECTORY_TO_WATCH)

    def on_created(self, event):
        logger.info(f"on_created event detected: {event.src_path}")
        self.process(event)

    @staticmethod
    def process_file(file_path, download_dir):
        if file_path.endswith('.json'):
            process_json_file(file_path, download_dir)
        else:
            uuid = fetch_custom_tag(file_path)
            if uuid:
                matched = match_scene_by_uuid(uuid, file_path)
                if matched:
                    logger.info(f"Automatically matched file {file_path} by UUID {uuid}")
                else:
                    logger.info(f"No match found for file {file_path} with UUID {uuid}")


def fetch_custom_tag(file_path):
    try:
        video = MP4(file_path)
        if "----:com.apple.iTunes:UUID" in video:
            return video["----:com.apple.iTunes:UUID"][0]  # Retrieve the custom UUID tag
        else:
            return None
    except Exception:
        return None


def match_scene_by_uuid(uuid, file_path):
    with app.app_context():
        engine = create_engine(f'sqlite:///{db_path}')
        session = sessionmaker(bind=engine)()

        try:
            matching_scene = session.query(Scene).filter_by(foreign_guid=uuid.decode('utf-8')).first()
            if matching_scene:
                matching_scene.local_path = file_path
                matching_scene.status = 'Found'
                session.commit()
                logger.info(f"Automatically matched scene ID: {matching_scene.id} with file: {file_path}")
                return True
            else:
                logger.info(f"No match found for UUID: {uuid}")
                return False
        except Exception as e:
            logger.error(f"Failed to match file {file_path} with UUID {uuid}: {e}")
            return False
        finally:
            session.close()


def process_json_file(json_file_path, download_dir):
    with app.app_context():
        engine = create_engine(f'sqlite:///{db_path}')
        session = sessionmaker(bind=engine)()

        try:
            json_file_path = str(json_file_path)  # Ensure the path is a string
            if json_file_path.endswith('.json'):
                with open(json_file_path, 'r') as f:
                    data = json.load(f)

                scene_url = data['URL']
                original_filename = data['filename']
                metadata = fetch_metadata(scene_url)

                if metadata:
                    new_filename = construct_filename(metadata, original_filename)
                    original_filepath = os.path.join(download_dir, original_filename)
                    new_filepath = os.path.join(download_dir, new_filename)

                    # Only rename if the original file exists
                    if os.path.exists(original_filepath):
                        try:
                            os.rename(original_filepath, new_filepath)
                            logger.info(f"Renamed {original_filename} to {new_filename}")

                            # Tag the file with metadata before moving
                            tag_file_with_metadata(new_filepath, metadata)
                            logger.info(f"Metadata tagged for file {new_filepath}")

                            # Update the JSON file with the new filename
                            data['filename'] = new_filename
                            new_json_filepath = os.path.join(download_dir, new_filename.replace(new_filename.split('.')[-1], 'json'))
                            os.rename(json_file_path, new_json_filepath)
                            with open(new_json_filepath, 'w') as f:
                                json.dump(data, f, indent=4)
                            logger.info(f"Updated and renamed JSON file {json_file_path} to {new_json_filepath} with new filename {new_filename}")

                            # Move the files and update paths
                            new_file_path, new_json_file_path = move_files_to_site_directory(new_filepath, new_json_filepath, metadata['site']['name'], session)

                            # Added delay after moving the file
                            time.sleep(2)

                            # Directly match the scene using the scene URL
                            if new_file_path:
                                match_scene_with_uuid(metadata['foreign_guid'], new_file_path, session)
                            else:
                                logger.error(f"Failed to move file {new_filepath}")

                            return new_filename
                        except Exception as e:
                            logger.error(f"Failed to rename file: {e}")
                    else:
                        logger.error(f"File {original_filename} not found.")
                        return None
                else:
                    logger.error("Failed to fetch metadata.")
                    return None
        finally:
            session.close()


def fetch_metadata(scene_url):
    response = requests.post('http://localhost:6900/get_metadata', json={'scene_url': scene_url})
    if response.status_code == 200:
        return response.json()
    else:
        logger.error(f"Failed to fetch metadata: {response.status_code}")
        return None


def construct_filename(metadata, original_filename):
    performers = ', '.join([performer['name'] for performer in metadata['performers']])
    file_extension = original_filename[original_filename.rfind('.'):]
    new_filename = f"{metadata['site']['name']} - {metadata['date']} - {metadata['title']} - {performers}"
    return sanitize_filename(new_filename + file_extension)


def tag_file_with_metadata(file_path, metadata):
    try:
        logger.info(f"Tagging file {file_path} with metadata: {metadata}")
        video = MP4(file_path)
        video_tags = video.tags or MP4Tags()
        video_tags["\xa9nam"] = metadata['title']
        video_tags["\xa9ART"] = ', '.join([performer['name'] for performer in metadata['performers']])
        video_tags["\xa9alb"] = metadata['site']['name']
        video_tags["\xa9day"] = metadata['date']
        video_tags["desc"] = metadata['site']['name']
        video_tags["----:com.apple.iTunes:UUID"] = [bytes(metadata['foreign_guid'], 'utf-8')]  # Add custom UUID tag
        video.tags = video_tags
        video.save()
    except KeyError as e:
        logger.error(f"Metadata missing key: {e}")
    except Exception as e:
        logger.error(f"Failed to tag metadata: {e}")


def sanitize_filename(filename):
    sanitized = re.sub(r'[<>:"/\\|?*]', '', filename)
    sanitized = re.sub(r'\s+', ' ', sanitized)
    sanitized = sanitized.strip()
    return sanitized


def move_files_to_site_directory(file_path, json_file_path, site_name, session):
    try:
        site = session.query(Site).filter_by(name=site_name).first()
        if not site or not site.home_directory:
            logger.error(f"Site {site_name} not found or has no home directory set.")
            return None, None

        try:
            destination_dir = Path(site.home_directory)
            destination_dir.mkdir(parents=True, exist_ok=True)

            new_file_path = destination_dir / Path(file_path).name
            new_json_file_path = destination_dir / Path(json_file_path).name

            shutil.move(file_path, new_file_path)
            shutil.move(json_file_path, new_json_file_path)

            logger.info(f"Moved {file_path} to {new_file_path}")
            logger.info(f"Moved {json_file_path} to {new_json_file_path}")

            return str(new_file_path), str(new_json_file_path)

        except Exception as e:
            logger.error(f"Failed to move files: {e}")
            return None, None
    finally:
        session.close()


def match_scene_with_uuid(uuid, file_path, session):
    try:
        logger.info(f"Matching file {file_path} with UUID {uuid}")
        scene = session.query(Scene).filter_by(foreign_guid=uuid).first()

        if scene:
            scene.local_path = file_path
            scene.status = 'Found'
            session.commit()
            logger.info(f"Automatically matched file {file_path} with UUID {uuid}")
        else:
            logger.error(f"No match found for UUID {uuid}")
    except Exception as e:
        logger.error(f"Failed to match file {file_path} with UUID {uuid}: {e}")


def main():
    with app.app_context():
        initialize_database()
        download_folder = Config.query.filter_by(key='downloadFolder').first()
        if download_folder:
            Watcher.DIRECTORY_TO_WATCH = download_folder.value
            watcher = Watcher()
            watcher.run()


if __name__ == '__main__':
    main()
