import os
import boto3
import sqlite3
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from flask_wtf import FlaskForm
from wtforms import StringField, PasswordField, SubmitField
from wtforms.validators import DataRequired, Length
from werkzeug.security import generate_password_hash, check_password_hash
from botocore.exceptions import ClientError
import tempfile
import paramiko
import logging
from datetime import datetime

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Initialize Flask app
app = Flask(__name__)
app.secret_key = 'your_secret_key'

# Initialize Flask-Login
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'  # Redirect users to the login page if not logged in

# Hardcoded values for SSH and root path
SSH_USER = "root"
SSH_KEY_PATH = "D:\\backup-script_app\\set_private1"  # Replace with the actual path to the private key
MONGO_DB_NAME = "test_db"  # Replace with your MongoDB name
ROOT_PATH = "/home/captain/application/sharklaravel"  # Hardcoded root path

# Load environment variables
AWS_ACCESS_KEY_ID = os.getenv('AWS_ACCESS_KEY_ID')
AWS_SECRET_ACCESS_KEY = os.getenv('AWS_SECRET_ACCESS_KEY')
BUCKET_NAME = os.getenv('BUCKET_NAME')
PREFIX = os.getenv('PREFIX', 'backups/')  # Default prefix

# S3 client initialization
s3_client = boto3.client(
    's3',
    aws_access_key_id=AWS_ACCESS_KEY_ID,
    aws_secret_access_key=AWS_SECRET_ACCESS_KEY
)

# SQLite database setup
def init_db():
    conn = sqlite3.connect('app_config.db')
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS users (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        username TEXT NOT NULL UNIQUE,
                        password TEXT NOT NULL
                    )''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS backups (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        project_name TEXT NOT NULL,
                        ssh_host TEXT NOT NULL,
                        backup_path TEXT NOT NULL,
                        root_path TEXT NOT NULL,
                        timestamp TEXT NOT NULL
                    )''')
    conn.commit()
    conn.close()

init_db()

# User Model
class User(UserMixin):
    def __init__(self, id, username, password):
        self.id = id
        self.username = username
        self.password = password

# Flask-Login User Loader
@login_manager.user_loader
def load_user(user_id):
    conn = sqlite3.connect('app_config.db')
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE id = ?", (user_id,))
    user = cursor.fetchone()
    conn.close()
    if user:
        return User(user[0], user[1], user[2])
    return None

# Login Form
class LoginForm(FlaskForm):
    username = StringField("Username", validators=[DataRequired(), Length(min=4, max=25)])
    password = PasswordField("Password", validators=[DataRequired()])
    submit = SubmitField("Login")

# Register Form
class RegisterForm(FlaskForm):
    username = StringField("Username", validators=[DataRequired(), Length(min=4, max=25)])
    password = PasswordField("Password", validators=[DataRequired()])
    submit = SubmitField("Register")

@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash("Logged out successfully!", "success")
    return redirect(url_for("login"))

# Perform backup and restore functions here (same as your previous implementation)


@app.route('/')
def home():
    # Default behavior for the root route
    if current_user.is_authenticated:
        flash("You are already logged in.", "info")
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))



@app.route('/dashboard', methods=['GET', 'POST'])
@login_required
def dashboard():
    # Logic for the dashboard functionality
    if request.method == 'POST':
        project_name = request.form.get('project_name')
        ssh_host = request.form.get('ssh_host')

        if not all([project_name, ssh_host]):
            flash('Please provide all required fields.', 'danger')
            return redirect(url_for('dashboard'))

        # Perform backup logic
        perform_backup(project_name, ssh_host)
        flash(f'Backup process initiated for project: {project_name}', 'success')
        return redirect(url_for('dashboard'))

    # Render the dashboard page
    return render_template('index.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    form = LoginForm()
    if form.validate_on_submit():
        username = form.username.data
        password = form.password.data

        conn = sqlite3.connect('app_config.db')
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE username = ?", (username,))
        user = cursor.fetchone()
        conn.close()

        if user and check_password_hash(user[2], password):
            login_user(User(user[0], user[1], user[2]))
            flash("Login successful!", "success")
            return redirect(url_for('dashboard'))  # Explicitly redirect to the dashboard
        flash("Invalid username or password!", "danger")
    return render_template("login.html", form=form)


@app.route('/register', methods=['GET', 'POST'])
def register():
    form = RegisterForm()
    if form.validate_on_submit():
        username = form.username.data
        password = generate_password_hash(form.password.data)

        conn = sqlite3.connect('app_config.db')
        cursor = conn.cursor()
        try:
            cursor.execute("INSERT INTO users (username, password) VALUES (?, ?)", (username, password))
            conn.commit()
            flash("User registered successfully!", "success")
            return redirect(url_for('login'))
        except sqlite3.IntegrityError:
            flash("Username already exists!", "danger")
        finally:
            conn.close()
    return render_template("register.html", form=form)

def perform_backup(project_name, ssh_host):
    try:
        root_path = ROOT_PATH

        with tempfile.TemporaryDirectory() as temp_dir:
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(ssh_host, username=SSH_USER, key_filename=SSH_KEY_PATH)
            logging.info(f"Connected to {ssh_host} as {SSH_USER}")

            sanitized_project_name = project_name.replace(" ", "_")
            combined_backup_path = "/tmp/combined_backup"
            ssh.exec_command(f"mkdir -p {combined_backup_path}")

            mongo_dump_command = f"mongodump --db {MONGO_DB_NAME} --out {combined_backup_path}/mongo_backup"
            stdin, stdout, stderr = ssh.exec_command(mongo_dump_command)
            if stdout.channel.recv_exit_status() != 0:
                raise RuntimeError(f"MongoDB dump failed: {stderr.read().decode()}")
            logging.info("MongoDB dump created.")

            app_copy_command = f"cp -r {root_path} {combined_backup_path}/application"
            stdin, stdout, stderr = ssh.exec_command(app_copy_command)
            if stdout.channel.recv_exit_status() != 0:
                raise RuntimeError(f"Application files copy failed: {stderr.read().decode()}")
            logging.info("Application files copied.")

            tar_filename = f"{sanitized_project_name}-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}.tar.gz"
            remote_tar_path = f"/tmp/{tar_filename}"
            tar_command = f"tar -czf {remote_tar_path} -C {combined_backup_path} ."
            stdin, stdout, stderr = ssh.exec_command(tar_command)
            if stdout.channel.recv_exit_status() != 0:
                raise RuntimeError(f"Tar command failed: {stderr.read().decode()}")
            logging.info(f"Backup archive created: {remote_tar_path}")

            local_tar_path = os.path.join(temp_dir, tar_filename)
            with ssh.open_sftp() as sftp:
                sftp.get(remote_tar_path, local_tar_path)

            upload_to_s3(local_tar_path, tar_filename, f"{PREFIX}{sanitized_project_name}/")

            conn = sqlite3.connect('app_config.db')
            cursor = conn.cursor()
            cursor.execute('''INSERT INTO backups (project_name, ssh_host, backup_path, root_path, timestamp)
                  VALUES (?, ?, ?, ?, ?)''',
               (project_name, ssh_host, f"{PREFIX}{sanitized_project_name}/{tar_filename}", 
                ROOT_PATH, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))

            conn.commit()
            conn.close()
            logging.info("Backup details saved to database.")

            cleanup_command = f"rm -rf {combined_backup_path} {remote_tar_path}"
            ssh.exec_command(cleanup_command)
            ssh.close()

    except Exception as e:
        logging.error(f"Error during backup: {e}")
        raise



@app.route('/restore', methods=['GET', 'POST'])
@login_required
def restore():
    if request.method == 'GET':
        # Fetch available projects and backups from S3
        try:
            objects = s3_client.list_objects(Bucket=BUCKET_NAME, Prefix=PREFIX).get('Contents', [])
            if not objects:
                flash('No backups found in S3.', 'info')
                return redirect(url_for('dashboard'))

            # Extract projects and backups properly
            projects = sorted(set(obj['Key'].split('/')[0] for obj in objects if '/' in obj['Key']))
            backups = [{'Key': obj['Key'], 'Name': obj['Key'].split('/')[-1]} for obj in objects if obj['Key']]
        except ClientError as e:
            logging.error(f"Error fetching backup list from S3: {e}")
            flash('Failed to fetch backups from S3.', 'error')
            return redirect(url_for('dashboard'))

        return render_template('restore.html', projects=projects, backups=backups)

    # Handle POST request for restore
    project_name = request.form.get('project_name')
    backup_key = request.form.get('backup_key')
    ssh_host = request.form.get('ssh_host')

    if not all([project_name, backup_key, ssh_host]):
        return jsonify({'success': False, 'message': 'All fields are required'}), 400

    try:
        # Perform pre-restoration backup
        logging.info("Initiating pre-restoration backup...")
        perform_backup(f"pre_restore_{project_name}", ssh_host)

        # Local and remote paths for the backup tarball
        local_tmp_dir = tempfile.mkdtemp()
        local_backup_path = os.path.join(local_tmp_dir, os.path.basename(backup_key))
        remote_tar_path = f"/tmp/{os.path.basename(backup_key)}"

        # Step 1: Download the backup tarball from S3
        s3_client.download_file(BUCKET_NAME, backup_key, local_backup_path)
        logging.info(f"Backup tarball downloaded to local path: {local_backup_path}")

        # Step 2: Upload the tarball to the remote server
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(ssh_host, username=SSH_USER, key_filename=SSH_KEY_PATH)

        with ssh.open_sftp() as sftp:
            sftp.put(local_backup_path, remote_tar_path)
        logging.info(f"Backup tarball uploaded to remote server at: {remote_tar_path}")

        # Step 3: Extract the tarball on the remote server
        extract_command = f"tar -xzvf {remote_tar_path} -C /tmp"
        stdin, stdout, stderr = ssh.exec_command(extract_command)
        if stdout.channel.recv_exit_status() != 0:
            raise RuntimeError(f"Extraction failed: {stderr.read().decode()}")
        logging.info(f"Backup tarball extracted on the remote server: /tmp")

        # Step 4: Restore MongoDB database
        mongo_restore_command = f"mongorestore --drop --dir=/tmp/mongo_backup"
        stdin, stdout, stderr = ssh.exec_command(mongo_restore_command)
        if stdout.channel.recv_exit_status() != 0:
            raise RuntimeError(f"MongoDB restore failed: {stderr.read().decode()}")
        logging.info("MongoDB database restored successfully.")

        # Step 5: Locate the correct `htdocs` directory
        locate_dir_command = "find /tmp/application -type d -name 'htdocs'"
        stdin, stdout, stderr = ssh.exec_command(locate_dir_command)
        all_dirs = stdout.read().decode().strip().split("\n")
        stderr_output = stderr.read().decode()

        if stderr_output:
            logging.error(f"Error locating 'htdocs': {stderr_output}")

        # Filter the correct directory path
        target_dir = None
        for dir_path in all_dirs:
            if dir_path.endswith("/htdocs"):
                target_dir = dir_path
                break

        if not target_dir:
            raise FileNotFoundError("Application directory (htdocs) not found in the extracted backup.")

        logging.info(f"Target application directory identified: {target_dir}")

        # Ensure ROOT_PATH exists
        check_root_path_command = f"mkdir -p {ROOT_PATH}"
        ssh.exec_command(check_root_path_command)

        # Copy the application files to ROOT_PATH
        app_restore_command = f"rsync -avz {target_dir}/ {ROOT_PATH}/htdocs"
        stdin, stdout, stderr = ssh.exec_command(app_restore_command)
        restore_stderr = stderr.read().decode()

        if stdout.channel.recv_exit_status() != 0 or restore_stderr:
            logging.error(f"Application files restoration failed: {restore_stderr}")
            raise RuntimeError(f"Application files restoration failed: {restore_stderr}")

        logging.info(f"Application files restored to: {ROOT_PATH}/htdocs")

        # Step 6: Clean up temporary files
        cleanup_command = f"rm -rf {remote_tar_path} /tmp/mongo_backup /tmp/application"
        ssh.exec_command(cleanup_command)
        ssh.close()

        # Clean up local temporary folder
        os.remove(local_backup_path)
        os.rmdir(local_tmp_dir)
        logging.info("Temporary files cleaned up.")

        return jsonify({'success': True, 'message': 'Restore completed successfully'})

    except Exception as e:
        logging.error(f"Error during restore: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500







def upload_to_s3(file_path, file_name, project_prefix):
    try:
        s3_client.upload_file(file_path, BUCKET_NAME, f"{project_prefix}{file_name}")
        logging.info(f"Successfully uploaded {file_name} to S3")
    except ClientError as e:
        logging.error(f"Error uploading to S3: {e}")


if __name__ == '__main__':
    app.run(debug=True)
