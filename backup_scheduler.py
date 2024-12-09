# backup_scheduler.py

import os
import subprocess
import zipfile
import boto3
from datetime import datetime
import schedule
import time
import threading
import tempfile
import json
import logging
from dotenv import load_dotenv
from botocore.exceptions import ClientError
from backup_utils import perform_backup, load_projects, save_projects

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Load environment variables
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

def manage_backups(project_prefix):
    response = s3_client.list_objects_v2(Bucket=BUCKET_NAME, Prefix=project_prefix)
    backups = response.get('Contents', [])

    if len(backups) > 11:
        backups.sort(key=lambda x: x['LastModified'])
        oldest_backup = backups[0]['Key']
        s3_client.delete_object(Bucket=BUCKET_NAME, Key=oldest_backup)
        logging.info(f"Deleted oldest backup: {oldest_backup}")

def upload_to_s3(temp_zip_path, zip_filename, project_prefix):
    try:
        with open(temp_zip_path, 'rb') as f:
            s3_client.upload_fileobj(f, BUCKET_NAME, f"{project_prefix}{zip_filename}")
        logging.info(f"Successfully uploaded {zip_filename} to S3")
    except ClientError as e:
        logging.error(f"Error uploading to S3: {e}")
    finally:
        os.unlink(temp_zip_path)



scheduled_jobs = {}

def schedule_backup(project_name, source_dir, db_user, db_password, db_name, backup_time):
    # Cancel any existing job for this project
    if project_name in scheduled_jobs:
        schedule.cancel_job(scheduled_jobs[project_name])

    # Schedule the new backup job
    job = schedule.every().day.at(backup_time).do(
        perform_backup, project_name, source_dir, db_user, db_password, db_name
    )
    scheduled_jobs[project_name] = job
    logging.info(f"Scheduled backup for project: {project_name} at {backup_time}")


def run_scheduler():
    while True:
        schedule.run_pending()
        time.sleep(1)



def initialize_scheduled_backups():
    projects = load_projects()
    for project_name, details in projects.items():
        schedule_backup(
            project_name,
            details['source_directory'],
            details['db_user'],
            details['db_password'],
            details['db_name'],
            details.get('backup_time', '00:00')  # Default to midnight if not specified

        )

if __name__ == '__main__':
    initialize_scheduled_backups()
    scheduler_thread = threading.Thread(target=run_scheduler)
    scheduler_thread.daemon = True
    scheduler_thread.start()
    logging.info("Backup scheduler service started")
    while True:
        time.sleep(1)