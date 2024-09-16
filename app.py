import os
import subprocess
import zipfile
import boto3
from datetime import datetime
import schedule
import time
import threading
from flask import Flask, render_template, request, redirect, url_for, flash
from botocore.exceptions import ClientError
import shutil
import logging
import tempfile

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Initialize Flask app
app = Flask(__name__)
app.secret_key = 'your_secret_key'

# AWS S3 configuration
AWS_ACCESS_KEY_ID = os.getenv('AWS_ACCESS_KEY_ID')
AWS_SECRET_ACCESS_KEY = os.getenv('AWS_SECRET_ACCESS_KEY')
BUCKET_NAME = os.getenv('BUCKET_NAME')
PREFIX = os.getenv('PREFIX')
PROJECT_NAME = os.getenv('PROJECT_NAME')
SAMPLE_DIRECTORY = os.getenv('SAMPLE_DIRECTORY')
RESTORE_DIRECTORY = SAMPLE_DIRECTORY

# S3 client initialization
s3_client = boto3.client(
    's3',
    aws_access_key_id=AWS_ACCESS_KEY_ID,
    aws_secret_access_key=AWS_SECRET_ACCESS_KEY
)

# Global variable to track if a backup is in progress
backup_in_progress = False
backup_lock = threading.Lock()

def create_db_backup(backup_dir):
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    db_backup_filename = os.path.join(backup_dir, f"{PROJECT_NAME}_db_backup_{timestamp}.sql")
    
    try:
        dump_command = f"mysqldump -u {os.getenv('DB_USER')} -p{os.getenv('DB_PASSWORD')} {os.getenv('DB_NAME')} > {db_backup_filename}"
        subprocess.run(dump_command, shell=True, check=True, stderr=subprocess.DEVNULL)
        logging.info(f"Database backup created: {db_backup_filename}")
        return db_backup_filename
    except subprocess.CalledProcessError as e:
        logging.error(f"Error creating DB backup: {e}")
        if os.path.exists(db_backup_filename):
            os.remove(db_backup_filename)
        return None

def create_backup_zip(source_dir, backup_dir):
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    zip_filename = f"{PROJECT_NAME}-backup-{timestamp}.zip"
    zip_path = os.path.join(backup_dir, zip_filename)

    with zipfile.ZipFile(zip_path, 'w') as zipf:
        for root, _, files in os.walk(source_dir):
            for file in files:
                if not file.endswith('.sql'):
                    file_path = os.path.join(root, file)
                    arcname = os.path.relpath(file_path, source_dir)
                    zipf.write(file_path, arcname)
        logging.info(f"Added files from {source_dir} to zip")

        db_backup_filename = create_db_backup(backup_dir)
        if db_backup_filename:
            zipf.write(db_backup_filename, os.path.basename(db_backup_filename))
            os.remove(db_backup_filename)
            logging.info(f"Added database backup to zip and removed temporary SQL file")

    return zip_path

def manage_backups():
    project_prefix = f"{PREFIX}{PROJECT_NAME}/"
    response = s3_client.list_objects_v2(Bucket=BUCKET_NAME, Prefix=project_prefix)
    backups = response.get('Contents', [])

    if len(backups) > 22:
        backups.sort(key=lambda x: x['LastModified'])
        oldest_backup = backups[0]['Key']
        s3_client.delete_object(Bucket=BUCKET_NAME, Key=oldest_backup)
        logging.info(f"Deleted oldest backup: {oldest_backup}")

def upload_to_s3(temp_zip_path, zip_filename):
    project_prefix = f"{PREFIX}{PROJECT_NAME}/"
    try:
        with open(temp_zip_path, 'rb') as f:
            s3_client.upload_fileobj(f, BUCKET_NAME, f"{project_prefix}{zip_filename}")
        logging.info(f"Successfully uploaded {zip_filename} to S3")
    except ClientError as e:
        logging.error(f"Error uploading to S3: {e}")
    finally:
        os.unlink(temp_zip_path)

def perform_backup():
    global backup_in_progress
    
    with backup_lock:
        if backup_in_progress:
            logging.info("A backup is already in progress. Skipping this backup.")
            return
        backup_in_progress = True

    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            logging.info(f"Created temporary directory for backup: {temp_dir}")
            zip_path = create_backup_zip(SAMPLE_DIRECTORY, temp_dir)
            zip_filename = os.path.basename(zip_path)
            upload_to_s3(zip_path, zip_filename)
            manage_backups()
            logging.info(f"Backup process completed successfully")
    except Exception as e:
        logging.error(f"Unexpected error during backup: {e}")
    finally:
        with backup_lock:
            backup_in_progress = False

def schedule_backup():
    schedule.every(1).days.do(perform_backup)
    while True:
        schedule.run_pending()
        time.sleep(1)



# Helper function to clear existing code or database based on user selection
def clear_existing_code_and_db(restore_option):
    # Clear existing code if "code" or "full" is selected
    if restore_option in ['code', 'full']:
        for root, dirs, files in os.walk(RESTORE_DIRECTORY, topdown=False):
            for name in files:
                os.remove(os.path.join(root, name))
            for name in dirs:
                os.rmdir(os.path.join(root, name))
        logging.info("Existing code cleared successfully")

    # Clear existing database if "db" or "full" is selected
    if restore_option in ['db', 'full']:
        try:
            clear_db_command = [
                "mysql",
                "-u", os.getenv('DB_USER'),
                "-p" + os.getenv('DB_PASSWORD'),
                "-e", f"DROP DATABASE IF EXISTS {os.getenv('DB_NAME')}; CREATE DATABASE {os.getenv('DB_NAME')};"
            ]
            result = subprocess.run(clear_db_command, capture_output=True, text=True, check=True)

            if result.returncode != 0:
                logging.error(f"Error clearing existing database. Exit code: {result.returncode}")
                logging.error(f"Error output: {result.stderr}")
            else:
                logging.info("Existing database cleared successfully")
        except subprocess.CalledProcessError as e:
            logging.error(f"Error clearing existing database: {e}")
        except Exception as e:
            logging.error(f"Unexpected error: {e}")


@app.route('/', methods=['GET', 'POST'])
def restore():
    temp_file_path = None
    if request.method == 'POST':
        backup_key = request.form.get('backup_key')
        restore_option = request.form.get('restore_option')

        if not backup_key:
            flash('Please select a backup file to restore.')
            return redirect(url_for('restore'))

        backup_file = os.path.basename(backup_key)
        temp_dir = tempfile.mkdtemp()

        try:
            perform_backup()  # Backup before restore

            clear_existing_code_and_db(restore_option)

            with tempfile.NamedTemporaryFile(delete=False) as temp_file:
                s3_client.download_fileobj(BUCKET_NAME, backup_key, temp_file)
                temp_file_path = temp_file.name

            with zipfile.ZipFile(temp_file_path, 'r') as zip_ref:
                zip_ref.extractall(temp_dir)

            if restore_option == 'full':
                restore_code(temp_dir, RESTORE_DIRECTORY)
                restore_db(temp_dir)
                flash(f'Full backup restored successfully from: {backup_file}')
            elif restore_option == 'db':
                restore_db(temp_dir)
                flash(f'Database restored successfully from: {backup_file}')
            elif restore_option == 'code':
                restore_code(temp_dir, RESTORE_DIRECTORY)
                flash(f'Code restored successfully from: {backup_file}')

        except ClientError as e:
            flash(f'Error during restore: {e}')
            logging.error(f'Error during restore: {e}')
        except Exception as e:
            flash(f'Unexpected error during restore: {e}')
            logging.error(f'Unexpected error during restore: {e}')
        finally:
            if temp_file_path and os.path.exists(temp_file_path):
                os.unlink(temp_file_path)
            shutil.rmtree(temp_dir)

        return redirect(url_for('restore'))

    response = s3_client.list_objects_v2(Bucket=BUCKET_NAME, Prefix=f"{PREFIX}{PROJECT_NAME}/")
    backups = response.get('Contents', [])
    backup_files = [{'Key': b['Key'], 'LastModified': b['LastModified']} for b in backups]

    return render_template('index.html', backups=backup_files)

def restore_code(source_dir, destination_dir):
    for root, _, files in os.walk(source_dir):
        for file in files:
            if not file.endswith('.sql'):
                file_path = os.path.join(root, file)
                rel_path = os.path.relpath(file_path, source_dir)
                dest_path = os.path.join(destination_dir, rel_path)
                os.makedirs(os.path.dirname(dest_path), exist_ok=True)
                shutil.copy2(file_path, dest_path)

def restore_db(temp_dir):
    for file in os.listdir(temp_dir):
        if file.endswith(".sql"):
            sql_file = os.path.join(temp_dir, file)
            try:
                restore_command = f"mysql -u {os.getenv('DB_USER')} -p{os.getenv('DB_PASSWORD')} {os.getenv('DB_NAME')} < {sql_file}"
                subprocess.run(restore_command, shell=True, check=True, stderr=subprocess.DEVNULL)
                logging.info(f"Database restored successfully from {file}")
            except subprocess.CalledProcessError as e:
                logging.error(f"Error restoring database: {e}")
            finally:
                os.remove(sql_file)
                logging.info(f"Removed SQL dump file: {file}")
            break

if __name__ == '__main__':
    backup_scheduler = threading.Thread(target=schedule_backup)
    backup_scheduler.daemon = True
    backup_scheduler.start()
    app.run(host='0.0.0.0', port=5000, threaded=True)

