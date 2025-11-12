import io
import os
import random
import string
import zipfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv
from flask import (
    Flask,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    send_from_directory,
    session,
    url_for,
)
from pymongo import DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError
from werkzeug.utils import secure_filename

ISTANBUL_TZ = ZoneInfo('Europe/Istanbul')
UTC_TZ = ZoneInfo('UTC')
UPLOAD_ROOT = Path(__file__).resolve().parent / 'uploads' / 'invoices'
UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
PHOTOS_ROOT = Path(__file__).resolve().parent / 'uploads' / 'photos'
PHOTOS_ROOT.mkdir(parents=True, exist_ok=True)

load_dotenv()

app = Flask(__name__)
app.config['JSON_AS_ASCII'] = False
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'dev-secret-key')

SMS_API_URL = os.environ.get('SMS_API_URL', 'https://smsvt.voicetelekom.com:9588/sms/create')
SMS_USER = os.environ.get('SMS_USER', '')
SMS_PASS = os.environ.get('SMS_PASS', '')
SMS_SENDER = os.environ.get('SMS_SENDER', '')

def _create_mongo_client():
    mongo_uri = os.environ.get('MONGO_URI', 'mongodb://localhost:27017/')
    return MongoClient(mongo_uri)


client = _create_mongo_client()
db_name = os.environ.get('MONGO_DB_NAME', 'sis-montaj')
db = client[db_name]
orders_collection = db['orders']
technicians_collection = db['technicians']

try:
    orders_collection.create_index('job_no', unique=True)
    orders_collection.create_index([('created_at', DESCENDING)])
    technicians_collection.create_index('username', unique=True)
except (PyMongoError, AttributeError, TypeError):
    app.logger.exception('MongoDB indeksleri oluşturulurken hata oluştu.')


def generate_job_no() -> str:
    while True:
        suffix = ''.join(random.choices(string.digits, k=4))
        job_no = f'TE-{suffix}'
        if not orders_collection.find_one({'job_no': job_no}):
            return job_no


def _ensure_datetime(value):
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC_TZ)
    return value.astimezone(UTC_TZ)


def normalize_text(value):
    if value is None:
        return ''
    return str(value).strip().upper()


def _unauthorized_json():
    return jsonify({'message': 'Oturum açılmadı.'}), 401


def _normalize_password(value):
    if value is None:
        return ''
    return str(value).strip().upper()


def has_admin_user() -> bool:
    try:
        return technicians_collection.count_documents({'level': 1}) > 0
    except Exception:
        return False


@app.before_request
def enforce_initial_setup():
    if has_admin_user():
        return
    endpoint = request.endpoint or ''
    allowed = {'setup_admin', 'static'}
    if endpoint in allowed or endpoint.startswith('static'):
        return
    return redirect(url_for('setup_admin'))


def _build_photo_entry(entry: dict) -> dict:
    if not entry:
        return {}
    stored_name = entry.get('stored_name')
    return {
        'original_name': entry.get('original_name', ''),
        'stored_name': stored_name or '',
        'uploaded_at': entry.get('uploaded_at').isoformat() if isinstance(entry.get('uploaded_at'), datetime) else '',
        'url': f"/photos/{stored_name}" if stored_name else ''
    }


def _save_photo(job_no: str, file_storage) -> dict:
    filename = secure_filename(file_storage.filename)
    if not filename:
        raise ValueError('Geçersiz dosya adı.')
    timestamp = datetime.now(UTC_TZ).strftime('%Y%m%d%H%M%S')
    random_part = ''.join(random.choices(string.ascii_uppercase + string.digits, k=4))
    stored_name = f"{job_no}-{timestamp}-{random_part}-{filename}"
    destination = PHOTOS_ROOT / stored_name
    file_storage.save(destination)
    return {
        'original_name': filename,
        'stored_name': stored_name,
        'uploaded_at': datetime.now(UTC_TZ).replace(tzinfo=None)
    }


def _send_sms(phone: str, content: str, custom_id: str = ''):
    phone = (phone or '').strip()
    content = (content or '').strip()
    if not (SMS_USER and SMS_PASS and SMS_SENDER and phone and content and SMS_API_URL):
        return
    if phone.startswith('+'):  # already international format
        formatted_number = phone
    else:
        digits = ''.join(filter(str.isdigit, phone))
        if digits.startswith('0'):
            digits = digits[1:]
        formatted_number = '+90' + digits if not digits.startswith('9') else '+' + digits
    payload = {
        'type': 1,
        'sendingType': 0,
        'title': 'Montaj Bilgilendirme',
        'content': content,
        'number': formatted_number,
        'encoding': 0,
        'sender': SMS_SENDER,
        'sendingDate': '',
        'validity': 60,
        'commercial': False,
        'skipAhsQuery': True,
        'recipientType': 0,
        'customID': custom_id[:64] if custom_id else None,
    }
    if not payload['customID']:
        payload.pop('customID')
    try:
        app.logger.info('SMS gönderimi: %s -> %s', formatted_number, content)
        response = requests.post(
            SMS_API_URL,
            headers={'Content-Type': 'application/json'},
            json=payload,
            auth=(SMS_USER, SMS_PASS),
            timeout=10,
        )
        response.raise_for_status()
        app.logger.info('SMS gönderildi: %s -> %s', formatted_number, response.text)
    except Exception as exc:
        app.logger.warning('SMS gönderimi başarısız: %s', exc)


def _notify_new_order(order_info: dict, original_data=None):
    if not order_info:
        return
    try:
        service_value = order_info.get('service') or ''
        display_service = service_value.title()
        raw_name = ''
        raw_phone = ''
        if original_data:
            raw_name = (original_data.get('name') or '').strip()
            raw_phone = (original_data.get('phone') or '').strip()
        customer_name = raw_name or order_info.get('name', '')
        phone = raw_phone or order_info.get('phone', '')
        if customer_name:
            customer_name_display = customer_name.title()
        else:
            customer_name_display = ''
        sms_message = f"Sayın {customer_name_display or order_info.get('name', '')}, {display_service} başvurunuz alınmıştır."
        if 'KURULUM' in service_value:
            token = job_no_to_token(order_info.get('job_no'))
            short_link = url_for('short_invoice_redirect', token=token, _external=True)
            sms_message += (
                f" Faturanızı en geç 24 saat içinde {short_link} üzerinden yükleyiniz. "
                "Faturası onaylanmayan işlemlerde servis planlaması yapılamaz."
            )
        else:
            sms_message += " Teknik ekibimiz en kısa sürede sizinle iletişime geçecektir."
        _send_sms(phone, sms_message, f"montaj_{order_info.get('job_no')}")
    except Exception as exc:
        app.logger.warning('Montaj SMS gönderimi başarısız: %s', exc)


def _store_invoice(job_no: str, file_storage):
    if not file_storage or not file_storage.filename:
        raise ValueError('Fatura dosyası seçilmedi.')
    filename = secure_filename(file_storage.filename)
    if not filename:
        raise ValueError('Geçersiz dosya adı.')
    timestamp = datetime.now(UTC_TZ).strftime('%Y%m%d%H%M%S')
    stored_name = f"{job_no}-{timestamp}-{filename}"
    destination = UPLOAD_ROOT / stored_name
    try:
        file_storage.save(destination)
    except OSError as exc:
        raise ValueError('Fatura kaydedilemedi.') from exc

    upload_time = datetime.now(UTC_TZ)
    try:
        orders_collection.update_one(
            {'job_no': job_no},
            {'$set': {
                'invoice': {
                    'original_name': filename,
                    'stored_name': stored_name,
                    'uploaded_at': upload_time.replace(tzinfo=None)
                }
            }}
        )
        updated = orders_collection.find_one({'job_no': job_no})
    except (PyMongoError, AttributeError):
        if destination.exists():
            destination.unlink(missing_ok=True)
        raise
    return updated


def job_no_to_token(job_no: str) -> str:
    normalized = normalize_text(job_no or '')
    return normalized.replace('-', '')


def token_to_job_no(token: str) -> str | None:
    if not token:
        return None
    sanitized = normalize_text(token)
    if len(sanitized) < 3:
        return None
    prefix = sanitized[:2]
    rest = sanitized[2:]
    if not prefix or not rest:
        return None
    return f"{prefix}-{rest}"


def _build_order_document(data: dict) -> dict:
    required_fields = ['priority', 'name', 'model', 'phone', 'service', 'address']
    missing = [field for field in required_fields if not (data.get(field) or '').strip()]
    if missing:
        raise ValueError('Eksik alanlar: ' + ', '.join(missing))

    priority_input = (data.get('priority') or 'DÜŞÜK').strip()
    priority = normalize_text(priority_input)
    if priority not in {'YÜKSEK', 'ORTA', 'DÜŞÜK'}:
        priority = 'DÜŞÜK'

    name = normalize_text(data.get('name'))
    model = normalize_text(data.get('model'))
    phone = normalize_text(data.get('phone'))
    service = normalize_text(data.get('service'))
    rnu = normalize_text(data.get('rnu'))
    address = normalize_text(data.get('address'))
    note = (data.get('note') or '').strip()

    job_no = generate_job_no()
    created_at = datetime.now(UTC_TZ)
    created_at_display = created_at.astimezone(ISTANBUL_TZ)
    document = {
        'job_no': job_no,
        'priority': priority,
        'name': name,
        'model': model,
        'phone': phone,
        'service': service,
        'rnu': rnu,
        'address': address,
        'note': note,
        'created_at': created_at.replace(tzinfo=None),
        'created_at_display': created_at_display.strftime('%d.%m.%Y %H:%M'),
        'invoice': None,
        'photos': [],
        'montaj_completed': False,
        'montaj_completion': None
    }
    return document


def create_order_from_payload(data: dict) -> dict:
    document = _build_order_document(data)
    try:
        result = orders_collection.insert_one(document)
        document['_id'] = result.inserted_id
    except DuplicateKeyError as exc:
        raise ValueError('İşemri numarası çakıştı, lütfen tekrar deneyin.') from exc
    return format_order(document)


def format_order(document: dict) -> dict:
    created_at = _ensure_datetime(document.get('created_at'))
    created_display_dt = created_at.astimezone(ISTANBUL_TZ) if created_at else None
    created_display = document.get('created_at_display')
    if not created_display and created_display_dt:
        created_display = created_display_dt.strftime('%d.%m.%Y %H:%M')

    priority = normalize_text(document.get('priority')) or 'DÜŞÜK'
    name = normalize_text(document.get('name'))
    model = normalize_text(document.get('model'))
    phone = normalize_text(document.get('phone'))
    service = normalize_text(document.get('service'))
    rnu = normalize_text(document.get('rnu'))
    address = normalize_text(document.get('address'))
    photos = document.get('photos') or []
    montaj_completed = bool(document.get('montaj_completed'))
    montaj_completion = document.get('montaj_completion') or {}

    return {
        'id': str(document.get('_id')) if document.get('_id') else None,
        'job_no': document.get('job_no', ''),
        'priority': priority,
        'name': name,
        'model': model,
        'phone': phone,
        'service': service,
        'rnu': rnu,
        'address': address,
        'note': document.get('note', ''),
        'created_at': created_at.isoformat() if created_at else '',
        'created_at_display': created_display or '',
        'invoice_uploaded': bool(document.get('invoice') and document['invoice'].get('stored_name')),
        'invoice_url': f"/invoices/{document['invoice']['stored_name']}" if (document.get('invoice') and document['invoice'].get('stored_name')) else '',
        'invoice': {
            'original_name': (document.get('invoice') or {}).get('original_name', ''),
            'stored_name': (document.get('invoice') or {}).get('stored_name', ''),
            'uploaded_at': ((document.get('invoice') or {}).get('uploaded_at') or '').isoformat()
            if isinstance((document.get('invoice') or {}).get('uploaded_at'), datetime) else ''
        } if document.get('invoice') else None,
        'photos': [_build_photo_entry(entry) for entry in photos if entry],
        'montaj_completed': montaj_completed,
        'montaj_completion': {
            'mount_type': normalize_text(montaj_completion.get('mount_type')),
            'note': montaj_completion.get('note', ''),
            'completed_at': montaj_completion.get('completed_at').isoformat()
            if isinstance(montaj_completion.get('completed_at'), datetime) else '',
            'photo_count': montaj_completion.get('photo_count', 0)
        } if montaj_completion else None
    }


@app.route('/')
def index():
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    if session.get('technician_level') == 3:
        return redirect(url_for('montaj_kapama'))
    return render_template('index.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if session.get('logged_in'):
        return redirect(url_for('index'))

    error = None
    if request.method == 'POST':
        username_input = request.form.get('username') or ''
        password_input = request.form.get('password') or ''
        username = normalize_text(username_input)
        password = _normalize_password(password_input)

        technician = None
        if username:
            technician = technicians_collection.find_one({'username': username})

        if technician and technician.get('password') == password:
            session['logged_in'] = True
            session['username'] = technician.get('username')
            session['technician_name'] = technician.get('name')
            session['technician_level'] = technician.get('level')
            if technician.get('level') == 3:
                return redirect(url_for('montaj_kapama'))
            return redirect(url_for('index'))

        error = 'Kullanıcı adı veya şifre hatalı.'

    last_username = ''
    if request.method == 'POST':
        last_username = request.form.get('username') or ''

    return render_template('login.html', error=error, last_username=last_username)


@app.route('/logout', methods=['POST'])
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/setup', methods=['GET', 'POST'])
def setup_admin():
    if has_admin_user():
        return redirect(url_for('login'))
    error = None
    if request.method == 'POST':
        full_name = normalize_text(request.form.get('name'))
        username = normalize_text(request.form.get('username'))
        password = _normalize_password(request.form.get('password'))
        if not full_name or not username or not password:
            error = 'Lütfen tüm alanları doldurun.'
        else:
            document = {
                'name': full_name,
                'username': username,
                'password': password,
                'level': 1,
                'created_at': datetime.now(UTC_TZ).replace(tzinfo=None),
            }
            try:
                result = technicians_collection.insert_one(document)
                session['logged_in'] = True
                session['username'] = username
                session['technician_name'] = full_name
                session['technician_level'] = 1
                return redirect(url_for('index'))
            except DuplicateKeyError:
                error = 'Bu kullanıcı adı zaten kullanılıyor.'
            except (PyMongoError, AttributeError):
                app.logger.exception('İlk admin oluşturulamadı.')
                error = 'Admin kaydı oluşturulamadı.'
    last_name = request.form.get('name', '') if request.method == 'POST' else ''
    last_username = request.form.get('username', '') if request.method == 'POST' else ''
    return render_template('setup.html', error=error, last_name=last_name, last_username=last_username)


@app.route('/api/orders', methods=['GET'])
def list_orders():
    if not session.get('logged_in'):
        return _unauthorized_json()
    try:
        documents = orders_collection.find().sort('created_at', DESCENDING)
        orders = [format_order(doc) for doc in documents]
        return jsonify(orders)
    except (PyMongoError, AttributeError):
        app.logger.exception('İş emri listesi alınamadı.')
        return jsonify({'message': 'Kayıtlar alınamadı.'}), 500


@app.route('/api/orders', methods=['POST'])
def create_order():
    if not session.get('logged_in'):
        return _unauthorized_json()
    data = request.get_json(silent=True) or {}
    try:
        created_order = create_order_from_payload(data)
    except ValueError as exc:
        return jsonify({'message': str(exc)}), 400
    except (PyMongoError, AttributeError):
        app.logger.exception('İş emri kaydı oluşturulamadı.')
        return jsonify({'message': 'Kayıt kaydedilemedi.'}), 500

    _notify_new_order(created_order, data)

    return jsonify({'order': created_order}), 201


@app.route('/api/orders/<job_no>/invoice', methods=['POST'])
def upload_invoice(job_no: str):
    if not session.get('logged_in'):
        return _unauthorized_json()
    order = orders_collection.find_one({'job_no': job_no})
    if not order:
        return jsonify({'message': 'İşemri bulunamadı.'}), 404

    if 'invoice' not in request.files:
        return jsonify({'message': 'Fatura dosyası bulunamadı.'}), 400

    try:
        updated = _store_invoice(job_no, request.files['invoice'])
    except ValueError as exc:
        return jsonify({'message': str(exc)}), 400
    except (PyMongoError, AttributeError):
        app.logger.exception('Fatura yüklenirken hata oluştu.')
        return jsonify({'message': 'Fatura yüklenemedi.'}), 500

    return jsonify({'order': format_order(updated), 'message': 'Fatura başarıyla yüklendi.'}), 201


@app.route('/api/orders/<job_no>/complete', methods=['POST'])
def complete_order(job_no: str):
    if not session.get('logged_in'):
        return _unauthorized_json()

    order = orders_collection.find_one({'job_no': job_no})
    if not order:
        return jsonify({'message': 'İşemri bulunamadı.'}), 404

    mount_type = normalize_text(request.form.get('mount_type') or '')
    if mount_type not in {'DUVAR', 'SEHPA', 'DIGER', ''}:
        return jsonify({'message': 'Geçersiz montaj türü.'}), 400
    note = (request.form.get('note') or '').strip()
    files = request.files.getlist('photos')

    saved_entries = []
    try:
        for file_storage in files:
            if file_storage and file_storage.filename:
                saved_entries.append(_save_photo(job_no, file_storage))
    except (ValueError, OSError) as exc:
        for entry in saved_entries:
            destination = PHOTOS_ROOT / entry.get('stored_name', '')
            if destination.exists():
                destination.unlink(missing_ok=True)
        return jsonify({'message': str(exc)}), 400

    if not saved_entries:
        return jsonify({'message': 'Lütfen en az bir fotoğraf yükleyin.'}), 400

    existing_photos = order.get('photos') or []
    total_photos = len(existing_photos) + len(saved_entries)

    update_doc = {
        'montaj_completed': True,
        'montaj_completion': {
            'mount_type': mount_type,
            'note': note,
            'photo_count': total_photos,
            'completed_at': datetime.now(UTC_TZ).replace(tzinfo=None)
        }
    }

    if saved_entries:
        update_doc['photos'] = existing_photos + saved_entries

    try:
        orders_collection.update_one({'job_no': job_no}, {'$set': update_doc})
        updated_order = orders_collection.find_one({'job_no': job_no})
    except (PyMongoError, AttributeError):
        app.logger.exception('Montaj kapatma kaydedilemedi.')
        return jsonify({'message': 'Montaj kapatılamadı.'}), 500

    return jsonify({'order': format_order(updated_order)}), 200


@app.route('/api/orders/<job_no>', methods=['PUT'])
def update_order(job_no: str):
    if not session.get('logged_in'):
        return _unauthorized_json()
    order = orders_collection.find_one({'job_no': job_no})
    if not order:
        return jsonify({'message': 'İşemri bulunamadı.'}), 404

    data = request.get_json(silent=True) or {}
    allowed_fields = {'priority', 'name', 'model', 'phone', 'service', 'rnu', 'address'}
    update_doc = {key: (data.get(key) or '').strip() for key in allowed_fields if key in data}

    if 'priority' in update_doc:
        valid_priorities = {'YÜKSEK', 'ORTA', 'DÜŞÜK'}
        priority_value = normalize_text(update_doc['priority'])
        if priority_value not in valid_priorities:
            priority_value = 'DÜŞÜK'
        update_doc['priority'] = priority_value

    text_fields = {'name', 'model', 'phone', 'service', 'rnu', 'address'}
    for field in text_fields:
        if field in update_doc:
            update_doc[field] = normalize_text(update_doc[field])

    if not update_doc:
        return jsonify({'message': 'Güncellenecek alan bulunamadı.'}), 400

    try:
        orders_collection.update_one({'job_no': job_no}, {'$set': update_doc})
        updated = orders_collection.find_one({'job_no': job_no})
    except (PyMongoError, AttributeError):
        app.logger.exception('İş emri güncellenemedi.')
        return jsonify({'message': 'Güncelleme başarısız.'}), 500

    return jsonify({'order': format_order(updated)}), 200


@app.route('/api/orders/<job_no>', methods=['DELETE'])
def delete_order(job_no: str):
    if not session.get('logged_in'):
        return _unauthorized_json()
    order = orders_collection.find_one({'job_no': job_no})
    if not order:
        return jsonify({'message': 'İşemri bulunamadı.'}), 404

    invoice_info = order.get('invoice') or {}
    stored_name = invoice_info.get('stored_name')
    if stored_name:
        destination = UPLOAD_ROOT / stored_name
        try:
            if destination.exists():
                destination.unlink()
        except OSError:
            app.logger.warning('Fatura dosyası silinemedi: %s', stored_name)

    for photo in order.get('photos') or []:
        photo_name = (photo or {}).get('stored_name')
        if not photo_name:
            continue
        photo_path = PHOTOS_ROOT / photo_name
        try:
            if photo_path.exists():
                photo_path.unlink()
        except OSError:
            app.logger.warning('Fotoğraf dosyası silinemedi: %s', photo_name)

    try:
        orders_collection.delete_one({'job_no': job_no})
    except (PyMongoError, AttributeError):
        app.logger.exception('İş emri silinemedi.')
        return jsonify({'message': 'Kayıt silinemedi.'}), 500

    return jsonify({'message': 'Kayıt silindi.'}), 200


def format_technician(document: dict) -> dict:
    return {
        'id': str(document.get('_id')) if document.get('_id') else None,
        'name': normalize_text(document.get('name')),
        'username': normalize_text(document.get('username')),
        'level': document.get('level'),
    }


@app.route('/api/technicians', methods=['POST'])
def create_technician():
    if not session.get('logged_in'):
        return _unauthorized_json()

    data = request.get_json(silent=True) or {}
    required_fields = ['name', 'username', 'password', 'level']
    missing = [field for field in required_fields if not (data.get(field) or '').strip()]
    if missing:
        return jsonify({'message': 'Eksik alanlar: ' + ', '.join(missing)}), 400

    name = normalize_text(data.get('name'))
    username = normalize_text(data.get('username'))
    password = _normalize_password(data.get('password'))

    try:
        level = int(data.get('level'))
    except (TypeError, ValueError):
        level = None

    valid_levels = {3, 4, 5}
    if level not in valid_levels:
        return jsonify({'message': 'Seçilen seviye geçersiz.'}), 400

    document = {
        'name': name,
        'username': username,
        'password': password,
        'level': level,
        'created_at': datetime.now(UTC_TZ).replace(tzinfo=None),
    }

    try:
        result = technicians_collection.insert_one(document)
        document['_id'] = result.inserted_id
    except DuplicateKeyError:
        return jsonify({'message': 'Kullanıcı adı zaten kayıtlı.'}), 409
    except (PyMongoError, AttributeError):
        app.logger.exception('Teknisyen kaydı oluşturulamadı.')
        return jsonify({'message': 'Teknisyen kaydedilemedi.'}), 500

    tech = format_technician(document)
    return jsonify({'technician': tech, 'message': 'Teknisyen oluşturuldu.'}), 201


@app.route('/invoices/<path:filename>')
def serve_invoice(filename):
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    return send_from_directory(UPLOAD_ROOT, filename, as_attachment=True)


@app.route('/photos/<path:filename>')
def serve_photo(filename):
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    return send_from_directory(PHOTOS_ROOT, filename, as_attachment=False)


@app.route('/montaj-kapama')
def montaj_kapama():
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    query = {
        'service': {'$regex': 'KURULUM', '$options': 'i'},
        '$or': [
            {'montaj_completed': {'$exists': False}},
            {'montaj_completed': {'$ne': True}}
        ]
    }
    try:
        documents = orders_collection.find(query).sort([('priority', DESCENDING), ('created_at', DESCENDING)])
        orders = [format_order(doc) for doc in documents]
    except (PyMongoError, AttributeError):
        app.logger.exception('Montaj kapama kayıtları alınamadı.')
        orders = []
    return render_template('montaj.html', orders=orders)


@app.route('/api/orders/<job_no>/photos/download', methods=['GET'])
def download_photos(job_no: str):
    if not session.get('logged_in'):
        return _unauthorized_json()

    order = orders_collection.find_one({'job_no': job_no})
    if not order:
        return jsonify({'message': 'İşemri bulunamadı.'}), 404

    photos = order.get('photos') or []
    valid_photos = [photo for photo in photos if (photo or {}).get('stored_name')]
    if not valid_photos:
        return jsonify({'message': 'Kaydedilmiş fotoğraf bulunamadı.'}), 404

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
        for photo in valid_photos:
            stored_name = photo.get('stored_name')
            original_name = photo.get('original_name') or stored_name
            filepath = PHOTOS_ROOT / stored_name
            if filepath.exists():
                safe_name = secure_filename(original_name) or stored_name
                zf.write(filepath, arcname=safe_name)

    buffer.seek(0)
    download_name = f"{job_no}-RESIMLER.zip"
    return send_file(buffer, mimetype='application/zip', as_attachment=True, download_name=download_name)


@app.route('/upload-invoice/<token>', methods=['GET', 'POST'])
def upload_invoice_form(token):
    job_no = token_to_job_no(token)
    if not job_no:
        return render_template('upload_invoice.html', error='Geçersiz bağlantı.'), 400
    order = orders_collection.find_one({'job_no': job_no})
    if not order:
        return render_template('upload_invoice.html', error='Kayıt bulunamadı.'), 404

    message = None
    error = None
    if request.method == 'POST':
        try:
            file_storage = request.files.get('invoice')
            updated = _store_invoice(job_no, file_storage)
            order = updated or orders_collection.find_one({'job_no': job_no})
            message = 'Faturanız yüklendi. Teşekkür ederiz.'
        except ValueError as exc:
            error = str(exc)
        except (PyMongoError, AttributeError):
            app.logger.exception('Fatura yükleme formunda hata oluştu.')
            error = 'Fatura yüklenemedi, lütfen tekrar deneyin.'

    return render_template('upload_invoice.html', order=format_order(order), message=message, error=error, token=token)


@app.route('/u/<token>')
def short_invoice_redirect(token):
    job_no = token_to_job_no(token)
    if not job_no:
        return render_template('upload_invoice.html', error='Geçersiz bağlantı.'), 400
    return redirect(url_for('upload_invoice_form', token=token))


def _create_order_from_bayi(form) -> dict:
    payload = {
        'priority': 'ORTA',
        'name': normalize_text(form.get('name')),
        'phone': normalize_text(form.get('phone')),
        'model': normalize_text(form.get('model')),
        'service': normalize_text(form.get('service')) or 'TV KURULUM',
        'address': normalize_text(form.get('address')),
        'rnu': '',
    }
    for key, value in payload.items():
        if key != 'rnu' and not value:
            raise ValueError(f"Eksik alan: {key}")
    payload['note'] = (form.get('note') or '').strip()
    return payload


@app.route('/bayi', methods=['GET'])
def bayi_panel():
    return render_template('bayi.html')


@app.route('/bayi/orders', methods=['POST'])
def bayi_create_order():
    try:
        data = _create_order_from_bayi(request.form)
        invoice_file = request.files.get('invoice')
        if not invoice_file or not invoice_file.filename:
            return jsonify({'message': 'Fatura dosyası zorunludur.'}), 400
        created_order = create_order_from_payload(data)
        updated = _store_invoice(created_order['job_no'], invoice_file)
        formatted = format_order(updated)
        return jsonify({'order': formatted})
    except ValueError as exc:
        return jsonify({'message': str(exc)}), 400
    except (PyMongoError, AttributeError):
        app.logger.exception('Bayi kaydı oluşturulamadı.')
        return jsonify({'message': 'Kayıt oluşturulamadı.'}), 500


if __name__ == '__main__':
    app.run(debug=True)
