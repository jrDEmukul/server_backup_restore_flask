# backup_utils.py

import os
import zipfile
import tempfile
import json
import logging
import boto3
from datetime import datetime
from botocore.exceptions import ClientError
import subprocess
import threading



# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Global variable to track if a backup is in progress
backup_in_progress = False
backup_lock = threading.Lock()


# Load environment variables
from dotenv import load_dotenv
load_dotenv()

# AWS S3 configuration
AWS_ACCESS_KEY_ID = os.getenv('AWS_ACCESS_KEY_ID')
AWS_SECRET_ACCESS_KEY = os.getenv('AWS_SECRET_ACCESS_KEY')
BUCKET_NAME = os.getenv('BUCKET_NAME')
PREFIX = os.getenv('PREFIX')

# S3 client initialization
s3_client = boto3.client(
    's3',
    aws_access_key_id=AWS_ACCESS_KEY_ID,
    aws_secret_access_key=AWS_SECRET_ACCESS_KEY
)

def create_db_backup(backup_dir, db_user, db_password, db_name):
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    db_backup_filename = os.path.join(backup_dir, f"db_backup_{timestamp}.sql")
    
    try:
        dump_command = f"mysqldump -u {db_user} -p{db_password} {db_name} > {db_backup_filename}"
        subprocess.run(dump_command, shell=True, check=True, stderr=subprocess.DEVNULL)
        logging.info(f"Database backup created: {db_backup_filename}")
        return db_backup_filename
    except subprocess.CalledProcessError as e:
        logging.error(f"Error creating DB backup: {e}")
        if os.path.exists(db_backup_filename):
            os.remove(db_backup_filename)
        return None
    except Exception as e:
        logging.error(f"Unexpected error during DB backup: {e}")
        if os.path.exists(db_backup_filename):
            os.remove(db_backup_filename)
        return None


def create_backup_zip(source_dir, backup_dir, db_user, db_password, db_name):
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    zip_filename = f"backup-{timestamp}.zip"
    zip_path = os.path.join(backup_dir, zip_filename)

    with zipfile.ZipFile(zip_path, 'w') as zipf:
        for root, _, files in os.walk(source_dir):
            for file in files:
                if not file.endswith('.sql'):
                    file_path = os.path.join(root, file)
                    arcname = os.path.relpath(file_path, source_dir)
                    zipf.write(file_path, arcname)
        logging.info(f"Added files from {source_dir} to zip")

        db_backup_filename = create_db_backup(backup_dir, db_user, db_password, db_name)
        if db_backup_filename:
            zipf.write(db_backup_filename, os.path.basename(db_backup_filename))
            os.remove(db_backup_filename)
            logging.info(f"Added database backup to zip and removed temporary SQL file")

    return zip_path

def upload_to_s3(temp_zip_path, zip_filename, project_prefix):
    try:
        with open(temp_zip_path, 'rb') as f:
            s3_client.upload_fileobj(f, BUCKET_NAME, f"{project_prefix}{zip_filename}")
        logging.info(f"Successfully uploaded {zip_filename} to S3")
    except ClientError as e:
        logging.error(f"Error uploading to S3: {e}")
    finally:
        os.unlink(temp_zip_path)

def manage_backups(project_prefix):
    response = s3_client.list_objects_v2(Bucket=BUCKET_NAME, Prefix=project_prefix)
    backups = response.get('Contents', [])

    if len(backups) > 22:
        backups.sort(key=lambda x: x['LastModified'])
        oldest_backup = backups[0]['Key']
        s3_client.delete_object(Bucket=BUCKET_NAME, Key=oldest_backup)
        logging.info(f"Deleted oldest backup: {oldest_backup}")



def load_projects():
    if os.path.exists('projects.json'):
        with open('projects.json', 'r') as f:
            return json.load(f)
    return {}

def save_projects(projects):
    with open('projects.json', 'w') as f:
        json.dump(projects, f, indent=4)

def perform_backup(project_name, source_dir, db_user, db_password, db_name, backup_time=None):
    global backup_in_progress
    
    project_prefix = f"{PREFIX}{project_name}/"
    
    with backup_lock:
        if backup_in_progress:
            logging.info(f"A backup is already in progress. Skipping backup for {project_name}.")
            return
        backup_in_progress = True

    try:
        current_time = datetime.now().strftime("%H:%M")
        if backup_time and current_time != backup_time:
            logging.info(f"Current time {current_time} doesn't match scheduled backup time {backup_time} for {project_name}. Skipping.")
            return

        with tempfile.TemporaryDirectory() as temp_dir:
            logging.info(f"Created temporary directory for backup of {project_name}: {temp_dir}")
            zip_path = create_backup_zip(source_dir, temp_dir, db_user, db_password, db_name)
            zip_filename = os.path.basename(zip_path)
            upload_to_s3(zip_path, zip_filename, project_prefix)
            manage_backups(project_prefix)
            logging.info(f"Backup process completed successfully for {project_name}")
    except Exception as e:
        logging.error(f"Unexpected error during backup of {project_name}: {e}")
    finally:
        with backup_lock:
            backup_in_progress = False