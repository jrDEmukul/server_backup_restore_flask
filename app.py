# app.py

import os
import shutil
import zipfile
from datetime import datetime
import boto3
from flask import Flask, render_template, request, redirect, url_for, flash
from botocore.exceptions import ClientError
import tempfile
import json
import threading
import logging
import subprocess
from flask import Flask, jsonify
from dotenv import load_dotenv
from backup_utils import perform_backup, load_projects, save_projects
from backup_scheduler import schedule_backup


# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Initialize Flask app
app = Flask(__name__)
app.secret_key = 'your_secret_key'

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

def load_projects():
    if os.path.exists('projects.json'):
        with open('projects.json', 'r') as f:
            return json.load(f)
    return {}



@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        project_name = request.form.get('project_name')
        source_dir = request.form.get('source_directory')
        db_user = request.form.get('db_user')
        db_password = request.form.get('db_password')
        db_name = request.form.get('db_name')
        backup_time = request.form.get('backup_time')  # New field for backup time


        if not all([project_name, source_dir, db_user, db_password, db_name, backup_time]):
            flash('Please provide all required fields.')
            return redirect(url_for('index'))
        
        # Validate backup_time format (HH:MM)
        try:
            datetime.strptime(backup_time, "%H:%M")
        except ValueError:
            flash('Invalid time format. Please use HH:MM.')
            return redirect(url_for('index'))

        # Save project details to a file or database (for simplicity, using JSON file here)
        projects = load_projects()
        projects[project_name] = {
            'source_directory': source_dir,
            'db_user': db_user,
            'db_password': db_password,
            'db_name': db_name,
            'backup_time': backup_time  

        }
        save_projects(projects)
        perform_backup(project_name, source_dir, db_user, db_password, db_name, backup_time)


        flash(f'Backup scheduled for project: {project_name} at {backup_time}')
        return redirect(url_for('index'))

    return render_template('index.html')

def backup_current_code(source_dir, project_name):
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_filename = f"{project_name}-pre-restore-backup-{timestamp}.zip"
    backup_path = os.path.join(tempfile.gettempdir(), backup_filename)
    
    with zipfile.ZipFile(backup_path, 'w') as zipf:
        for root, _, files in os.walk(source_dir):
            for file in files:
                file_path = os.path.join(root, file)
                arcname = os.path.relpath(file_path, source_dir)
                zipf.write(file_path, arcname)
    
    project_prefix = f"{PREFIX}{project_name}/"
    s3_key = f"{project_prefix}{backup_filename}"
    
    try:
        s3_client.upload_file(backup_path, BUCKET_NAME, s3_key)
        logging.info(f"Pre-restore backup created and uploaded: {s3_key}")
    except ClientError as e:
        logging.error(f"Error uploading pre-restore backup: {e}")
    finally:
        os.remove(backup_path)


def get_projects_from_s3():
    try:
        response = s3_client.list_objects_v2(Bucket=BUCKET_NAME, Prefix=PREFIX)
        
        projects = set()
        for item in response.get('Contents', []):
            key = item['Key']
            parts = key.split('/')
            if len(parts) > 2:  # Ensure we have PREFIX/project_name/...
                projects.add(parts[1])  # Add the project name
        
        projects = list(projects)
        return projects
    except ClientError as e:
        logging.error(f"Error getting projects from S3: {e}")
        return []

@app.route('/delete_project', methods=['POST'])
def delete_project():
    project_name = request.form.get('project_name')
    if not project_name:
        return jsonify({'success': False, 'message': 'Project name is required'}), 400

    # Delete project from S3
    project_prefix = f"{PREFIX}{project_name}/"
    try:
        # List all objects with the project prefix
        response = s3_client.list_objects_v2(Bucket=BUCKET_NAME, Prefix=project_prefix)
        
        # Delete each object
        for obj in response.get('Contents', []):
            s3_client.delete_object(Bucket=BUCKET_NAME, Key=obj['Key'])
        
        # Delete the project from JSON file
        projects = load_projects()
        if project_name in projects:
            del projects[project_name]
            save_projects(projects)
        
        logging.info(f"Project {project_name} deleted successfully from S3 and JSON")
        return jsonify({'success': True, 'message': f'Project {project_name} deleted successfully'})
    except ClientError as e:
        logging.error(f"Error deleting project from S3: {e}")
        return jsonify({'success': False, 'message': f'Error deleting project: {str(e)}'}), 500
    except Exception as e:
        logging.error(f"Unexpected error deleting project: {e}")
        return jsonify({'success': False, 'message': f'Unexpected error: {str(e)}'}), 500

@app.route('/restore', methods=['GET', 'POST'])
def restore():
    projects = get_projects_from_s3()
    
    if not projects:
        flash("No projects found in S3.")
        return render_template('restore.html', projects=[], selected_project=None, backups=[])

    selected_project = request.args.get('project_name') or request.form.get('project_name') or (projects[0] if projects else None)
    
    backups = []
    if selected_project:
        project_prefix = f"{PREFIX}{selected_project}/"
        try:
            response = s3_client.list_objects_v2(Bucket=BUCKET_NAME, Prefix=project_prefix)
            backups = [
                {
                    'Key': item['Key'],
                    'LastModified': item['LastModified'].strftime('%Y-%m-%d %H:%M:%S')
                }
                for item in response.get('Contents', [])
                if item['Key'].lower().endswith('.zip')
            ]
        except ClientError as e:
            flash(f'Error listing backups: {e}')
            logging.error(f'Error listing backups: {e}')

    if request.method == 'POST':
        project_name = request.form.get('project_name')
        backup_key = request.form.get('backup_key')
        restore_option = request.form.get('restore_option')

        if not project_name or not backup_key:
            flash('Please select both a project and a backup file to restore.')
            return redirect(url_for('restore'))

        project_prefix = f"{PREFIX}{project_name}/"
        backup_file = os.path.basename(backup_key)
        temp_dir = tempfile.mkdtemp()

        try:
            project_details = load_projects().get(project_name)
            if project_details:
                # Backup current code before restoration
                if restore_option in ['full', 'code']:
                    backup_current_code(project_details['source_directory'], project_name)

                # Download the backup file
                with tempfile.NamedTemporaryFile(delete=False) as temp_file:
                    s3_client.download_fileobj(BUCKET_NAME, backup_key, temp_file)
                    temp_file_path = temp_file.name

                # Extract the backup
                with zipfile.ZipFile(temp_file_path, 'r') as zip_ref:
                    zip_ref.extractall(temp_dir)

                # Clear existing code and database based on restore option
                clear_existing_code_and_db(
                    restore_option,
                    project_details['source_directory'],
                    project_details['db_user'],
                    project_details['db_password'],
                    project_details['db_name']
                )

                # Perform restore based on selected option
                if restore_option in ['full', 'code']:
                    restore_code(temp_dir, project_details['source_directory'])
                if restore_option in ['full', 'db']:
                    restore_db(
                        temp_dir,
                        project_details['db_user'],
                        project_details['db_password'],
                        project_details['db_name']
                    )

                flash(f'Restore completed successfully from: {backup_file}')
            else:
                flash(f'Project "{project_name}" not found in local configuration. Please add it first.')

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

    # Default to showing backups for the first project, or let the user select
    selected_project = request.args.get('project_name', projects[0] if projects else None)
    project_prefix = f"{PREFIX}{selected_project}/" if selected_project else ""

    # List available backups for the selected project
    backups = []
    if project_prefix:
        try:
            response = s3_client.list_objects_v2(Bucket=BUCKET_NAME, Prefix=project_prefix)
            backups = response.get('Contents', [])
        except ClientError as e:
            flash(f'Error listing backups: {e}')
            logging.error(f'Error listing backups: {e}')

    # Convert datetime objects to string
    backup_files = [{'Key': b['Key'], 'LastModified': b['LastModified'].isoformat()} for b in backups]

    return render_template('restore.html', projects=projects, selected_project=selected_project, backups=backup_files)



def clear_existing_code_and_db(restore_option, source_dir, db_user, db_password, db_name):
    if restore_option in ['code', 'full']:
        for root, dirs, files in os.walk(source_dir, topdown=False):
            for name in files:
                os.remove(os.path.join(root, name))
            for name in dirs:
                os.rmdir(os.path.join(root, name))
        logging.info("Existing code cleared successfully")

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


def restore_code(temp_dir, target_dir):
    for root, _, files in os.walk(temp_dir):
        for file in files:
            if not file.endswith('.sql'):
                file_path = os.path.join(root, file)
                rel_path = os.path.relpath(file_path, temp_dir)
                dest_path = os.path.join(target_dir, rel_path)
                os.makedirs(os.path.dirname(dest_path), exist_ok=True)
                shutil.copy2(file_path, dest_path)
    logging.info(f"Code restored successfully from {temp_dir} to {target_dir}")

def restore_db(temp_dir, db_user, db_password, db_name):
    for file in os.listdir(temp_dir):
        if file.endswith(".sql"):
            sql_file = os.path.join(temp_dir, file)
            try:
                restore_command = f"mysql -u {db_user} -p{db_password} {db_name} < {sql_file}"
                subprocess.run(restore_command, shell=True, check=True, stderr=subprocess.DEVNULL)
                logging.info(f"Database restored successfully from {file}")
            except subprocess.CalledProcessError as e:
                logging.error(f"Error restoring database: {e}")
            finally:
                os.remove(sql_file)
                logging.info(f"Removed SQL dump file: {file}")
            break

if __name__ == '__main__':
    app.run(debug=True)
