from flask import Flask, request, jsonify, Response
from minio_access import remove_bucket, remove_object, download_last_object, get_url_last_object, list_objects, upload_object, create_bucket, get_object, print_info_object, download_inlumen_object, read_object_bytes
from auth_middleware import require_auth
from minio_gateway import get_minio_client
import os
import datetime
import mimetypes
import tempfile
from runtime_config import add_cors_headers
from workspace_storage import (
    bucket_belongs_to_workspace,
    node_bucket_name,
    version_snapshot_bucket,
    workspace_bucket_prefix,
)

app = Flask(__name__)

# Apply the CORS function to all routes using the after_request decorator
@app.after_request
def apply_cors(response):
    return add_cors_headers(response, request.headers.get("Origin"))


@app.route('/health', methods=['GET'])
def health():
    try:
        get_minio_client().list_buckets()
    except Exception as exc:
        return jsonify({'status': 'unavailable', 'details': str(exc)}), 503
    return jsonify({'status': 'ok'}), 200

# Add file to MinIO
@app.route('/minio_upload_file', methods=['POST'])
@require_auth
def minio_upload_file():
    if 'file' not in request.files:
        return jsonify({'status': 400})
    file = request.files['file']
    bucket_id = node_bucket_name(request.form.get('bucket_id'))
    # Process the file as needed (e.g., save to database or storage)
    file_name = file.filename
    descriptor, file_path = tempfile.mkstemp(prefix="inlumen-upload-")
    os.close(descriptor)
    file.save(file_path)
    try:
        # First, make sure bucket exists
        print(f"[minio_api.py] Check if bucket with ID: {bucket_id} exists. If not, creates this bucket.")
        create_bucket(bucket_name=bucket_id)
        # Second, upload file to MinIO
        print(f"[minio_api.py] Received request to load file {file.filename} to bucket {bucket_id}")
        upload_object(bucket_id, file_name, file_path)
        now = datetime.datetime.now()
    except Exception as e:
        return jsonify({'status': 500, 'error': 'Failed to upload to MinIO', 'details': str(e)}), 500
    finally:
        # Clean up the temporary file after upload
        if os.path.exists(file_path):
            os.remove(file_path)
    return jsonify({'status': 200, 'file_name':file_name, 'add_date':now.strftime("%Y-%m-%d %H:%M:%S"), 'format':file_name.split(".")[-1]})

@app.route('/minio_read_file', methods=['GET'])
@require_auth
def minio_read_file():
    filename = request.args.get('filename', '').strip()
    requested_bucket = str(request.args.get('bucket_name') or '').strip().lower()
    if requested_bucket and not bucket_belongs_to_workspace(requested_bucket):
        return jsonify({'status': 404, 'error': 'bucket not found'}), 404
    bucket_id = requested_bucket or node_bucket_name(request.args.get('bucket_id'))
    if not filename:
        return jsonify({'status': 400, 'error': 'filename is required'}), 400
    try:
        content = read_object_bytes(bucket_id, filename)
    except Exception as e:
        return jsonify({'status': 500, 'error': 'Failed to read file from MinIO', 'details': str(e)}), 500
    content_type = mimetypes.guess_type(filename)[0] or 'application/octet-stream'
    return Response(content, mimetype=content_type)

@app.route('/minio_update_text_file', methods=['PUT', 'OPTIONS'])
@require_auth
def minio_update_text_file():
    if request.method == 'OPTIONS':
        return jsonify({}), 200
    data = request.get_json(silent=True) or {}
    filename = str(data.get('filename') or '').strip()
    content = data.get('content')
    bucket_id = node_bucket_name(data.get('bucket_id'))
    if not filename:
        return jsonify({'status': 400, 'error': 'filename is required'}), 400
    if not isinstance(content, str):
        return jsonify({'status': 400, 'error': 'content must be a string'}), 400
    try:
        create_bucket(bucket_name=bucket_id)
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', delete=False) as temp_file:
            temp_file.write(content)
            temp_path = temp_file.name
        upload_object(bucket_id, filename, temp_path)
        now = datetime.datetime.now()
    except Exception as e:
        return jsonify({'status': 500, 'error': 'Failed to update file in MinIO', 'details': str(e)}), 500
    finally:
        if 'temp_path' in locals() and os.path.exists(temp_path):
            os.remove(temp_path)
    return jsonify({'status': 200, 'file_name': filename, 'update_date': now.strftime("%Y-%m-%d %H:%M:%S"), 'format': filename.split(".")[-1]})

# Remove file from MinIO
@app.route('/minio_remove_file', methods=['DELETE', 'OPTIONS'])
@require_auth
def minio_remove_file():
    if request.method == 'OPTIONS':
        return jsonify({}), 200
    filename = request.form.get('filename')
    bucket_id = node_bucket_name(request.form.get('bucket_id'))
    try:
        # Remove file to MinIO
        print(f"[minio_api.py] Received request to remove file {filename} from bucket {bucket_id}")
        remove_object(bucket_id, filename)
        # List objects left in bucket, if empty --> remove bucket
        objects_in_bucket = list_objects(bucket_name=bucket_id)
        bucket_size = len(list(objects_in_bucket))
        if bucket_size == 0:
            remove_bucket(bucket_id)
            print(f"[minio_api.py] Removed bucket {bucket_id} due to empty state.")
        now = datetime.datetime.now()
    except Exception as e:
        return jsonify({'status': 500, 'error': 'Failed to remove file from MinIO', 'details': str(e)}), 500
    return jsonify({'status': 200, 'file_name':filename, 'removal_date':now.strftime("%Y-%m-%d %H:%M:%S"), 'format':filename.split(".")[-1]})

# Remove bucket from MinIO
@app.route('/minio_clear_bucket', methods=['DELETE', 'OPTIONS'])
@require_auth
def minio_clear_bucket():
    if request.method == 'OPTIONS':
        return jsonify({}), 200
    bucket_id = node_bucket_name(request.args.get('bucket_id'))
    try:
        # Remove file to MinIO
        print(f"[minio_api.py] Received request to remove bucket content from bucket {bucket_id}")
        remove_bucket(bucket_id)
        now = datetime.datetime.now()
    except Exception as e:
        return jsonify({'status': 500, 'error': 'Failed to remove bucket from MinIO', 'details': str(e)}), 500
    return jsonify({'status': 200, 'clear_date':now.strftime("%Y-%m-%d %H:%M:%S")})


def _workspace_bucket_names(client) -> list[str]:
    prefix = workspace_bucket_prefix()
    snapshots = version_snapshot_bucket()
    return sorted(
        bucket.name
        for bucket in client.list_buckets()
        if bucket.name == snapshots or bucket.name.startswith(prefix)
    )


def _remove_bucket_strict(client, bucket_name: str) -> None:
    for item in client.list_objects(bucket_name, recursive=True):
        client.remove_object(bucket_name, item.object_name)
    client.remove_bucket(bucket_name)


@app.route('/minio_clear_workspace', methods=['DELETE', 'OPTIONS'])
@require_auth
def minio_clear_workspace():
    if request.method == 'OPTIONS':
        return jsonify({}), 200
    client = get_minio_client()
    deleted_buckets = []
    failures = []
    for bucket_name in _workspace_bucket_names(client):
        try:
            _remove_bucket_strict(client, bucket_name)
            deleted_buckets.append(bucket_name)
        except Exception as exc:
            failures.append({"bucket": bucket_name, "error": str(exc)})
    payload = {
        "status": "ok" if not failures else "partial",
        "deleted_buckets": deleted_buckets,
        "deleted_bucket_count": len(deleted_buckets),
        "failures": failures,
    }
    return jsonify(payload), 200 if not failures else 500

@app.route('/minio_local_download', methods=['GET'])
@require_auth
def minio_local_download():
    bucket_name = str(request.args.get('bucket_id') or '').lower()
    if not bucket_belongs_to_workspace(bucket_name):
        return jsonify({'status': 404, 'error': 'bucket not found'}), 404
    # Create temp dir if not present from before:
    download_dir = "./downloads"
    if not os.path.exists(download_dir):
        os.makedirs(download_dir)
    print(f"[minio_api.py] Received request to locally download MinIO data for latest file in bucket: ", bucket_name, " and save on path: ", download_dir)
    download_last_object(bucket_name=bucket_name, file_path=download_dir) 
    return jsonify({'status': 200})

@app.route('/minio_inlumen_download', methods=['GET'])
@require_auth
def minio_inlumen_download():
    bucket_name = str(request.args.get('bucket_id') or '').lower()
    if not bucket_belongs_to_workspace(bucket_name):
        return jsonify({'status': 404, 'error': 'bucket not found'}), 404
    # Create temp dir if not present from before:
    download_dir = "./downloads"
    if not os.path.exists(download_dir):
        os.makedirs(download_dir)
    print(f"[minio_api.py] Received request to locally download MinIO data for latest file in bucket: ", bucket_name, " and save on path: ", download_dir)
    file_path = download_inlumen_object(bucket_name=bucket_name, file_path=download_dir) 
    return jsonify({'status': 200, 'file_path': file_path})

@app.route('/minio_list_objects', methods=['GET'])
@require_auth
def minio_list_objects():
    bucket_name = request.args.get('bucket_name')  
    if not bucket_belongs_to_workspace(bucket_name):
        return jsonify({'status': 404, 'error': 'bucket not found'}), 404
    objects = list_objects(bucket_name=bucket_name)
    object_list = list(objects)
    object_list = [object.object_name for object in object_list]
    print(object_list)
    return jsonify({'objects': object_list})
    
@app.route('/minio_create_bucket', methods=['GET'])
@require_auth
def minio_create_bucket():
    bucket_name = request.args.get('bucket_name')  
    if not bucket_belongs_to_workspace(bucket_name):
        return jsonify({'status': 404, 'error': 'bucket not found'}), 404
    create_bucket(bucket_name=bucket_name)
    print("[minio_api.py] Bucket "+ bucket_name + " created.")
    return jsonify({'status': 200})

@app.route('/minio_get_object', methods=['GET'])
@require_auth
def minio_get_object():
    bucket_name = request.args.get('bucket_name')
    if not bucket_belongs_to_workspace(bucket_name):
        return jsonify({'status': 404, 'error': 'bucket not found'}), 404
    object_name = request.args.get('object_name') 
    prefix = request.args.get('prefix') 
    object_response = get_object(bucket_name=bucket_name, object_name = object_name, prefix=prefix)
    print("[minio_api.py] Object returned.")
    return jsonify({'object': object_response})
