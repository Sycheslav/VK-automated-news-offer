"""
VK Suggester - Веб-приложение для массовой отправки постов в предложку ВК.
"""
import os
import json
import threading
import queue
import time
from datetime import datetime
from flask import Flask, render_template, request, jsonify, Response
from werkzeug.utils import secure_filename

from vk_suggester import VKSuggester, VKApiError, generate_oauth_url, PostStatus

app = Flask(__name__)
app.config['SECRET_KEY'] = os.urandom(24)
app.config['UPLOAD_FOLDER'] = os.path.join(os.path.dirname(__file__), 'uploads')
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50 MB max

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}

# Глобальные переменные для SSE логов
log_queues = {}
active_tasks = {}


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


@app.route('/')
def index():
    """Главная страница."""
    return render_template('index.html')


@app.route('/api/generate-oauth-url', methods=['POST'])
def api_generate_oauth_url():
    """Генерация URL для получения токена."""
    data = request.get_json()
    client_id = data.get('client_id', 6121396)  # ID по умолчанию
    
    try:
        client_id = int(client_id)
    except (ValueError, TypeError):
        return jsonify({'error': 'Некорректный ID приложения'}), 400
    
    url = generate_oauth_url(client_id)
    return jsonify({'url': url})


@app.route('/api/verify-token', methods=['POST'])
def api_verify_token():
    """Проверка токена и получение информации о пользователе."""
    data = request.get_json()
    token = data.get('token', '').strip()
    
    if not token:
        return jsonify({'error': 'Токен не указан'}), 400
    
    try:
        suggester = VKSuggester(token)
        user_info = suggester.get_user_info()
        return jsonify({
            'success': True,
            'user': {
                'id': user_info.user_id,
                'name': user_info.full_name
            }
        })
    except VKApiError as e:
        return jsonify({
            'success': False,
            'error': f'Ошибка VK API: {e.message}'
        }), 400
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'Ошибка проверки токена: {str(e)}'
        }), 400


@app.route('/api/upload-photo', methods=['POST'])
def api_upload_photo():
    """Загрузка фото на сервер (локально)."""
    if 'photo' not in request.files:
        return jsonify({'error': 'Файл не найден'}), 400
    
    file = request.files['photo']
    if file.filename == '':
        return jsonify({'error': 'Файл не выбран'}), 400
    
    if not allowed_file(file.filename):
        return jsonify({'error': 'Недопустимый формат файла'}), 400
    
    # Сохраняем локально с уникальным именем
    filename = secure_filename(file.filename)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S_')
    filename = timestamp + filename
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
    file.save(filepath)
    
    return jsonify({
        'success': True,
        'filename': filename,
        'filepath': filepath
    })


@app.route('/api/remove-photo', methods=['POST'])
def api_remove_photo():
    """Удаление загруженного фото."""
    data = request.get_json()
    filename = data.get('filename', '')
    
    if filename:
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], secure_filename(filename))
        if os.path.exists(filepath):
            os.remove(filepath)
    
    return jsonify({'success': True})


@app.route('/api/start-posting', methods=['POST'])
def api_start_posting():
    """Запуск процесса отправки постов."""
    data = request.get_json()
    
    token = data.get('token', '').strip()
    message = data.get('message', '').strip()
    groups_text = data.get('groups', '').strip()
    photos = data.get('photos', [])
    delay = float(data.get('delay', 0.5))
    
    if not token:
        return jsonify({'error': 'Токен не указан'}), 400
    
    if not message and not photos:
        return jsonify({'error': 'Укажите текст или добавьте фото'}), 400
    
    if not groups_text:
        return jsonify({'error': 'Укажите список групп'}), 400
    
    # Парсим список групп
    groups = []
    for sep in ['\n', ',', ';', ' ']:
        if sep in groups_text:
            groups = [g.strip() for g in groups_text.replace('\n', sep).split(sep) if g.strip()]
            break
    if not groups:
        groups = [groups_text.strip()]
    
    # Создаём уникальный ID задачи
    task_id = datetime.now().strftime('%Y%m%d_%H%M%S_') + os.urandom(4).hex()
    
    # Создаём очередь для логов
    log_queues[task_id] = queue.Queue()
    active_tasks[task_id] = {'status': 'running', 'stop': False}
    
    # Запускаем в отдельном потоке
    thread = threading.Thread(
        target=run_posting_task,
        args=(task_id, token, message, groups, photos, delay)
    )
    thread.daemon = True
    thread.start()
    
    return jsonify({
        'success': True,
        'task_id': task_id,
        'groups_count': len(groups)
    })


@app.route('/api/stop-posting', methods=['POST'])
def api_stop_posting():
    """Остановка процесса отправки."""
    data = request.get_json()
    task_id = data.get('task_id')
    
    if task_id and task_id in active_tasks:
        active_tasks[task_id]['stop'] = True
        return jsonify({'success': True})
    
    return jsonify({'error': 'Задача не найдена'}), 404


@app.route('/api/logs/<task_id>')
def api_logs_stream(task_id):
    """SSE поток логов."""
    def generate():
        if task_id not in log_queues:
            yield f"data: {json.dumps({'type': 'error', 'message': 'Задача не найдена'})}\n\n"
            return
        
        q = log_queues[task_id]
        
        while True:
            try:
                # Ждём сообщение с таймаутом
                msg = q.get(timeout=30)
                yield f"data: {json.dumps(msg)}\n\n"
                
                # Если это финальное сообщение - выходим
                if msg.get('type') == 'complete':
                    break
                    
            except queue.Empty:
                # Отправляем keepalive
                yield f"data: {json.dumps({'type': 'keepalive'})}\n\n"
    
    return Response(
        generate(),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no'
        }
    )


def run_posting_task(task_id, token, message, groups, photos, delay):
    """Выполнение задачи отправки постов в отдельном потоке."""
    q = log_queues[task_id]
    
    def log_callback(msg, level='info'):
        q.put({
            'type': 'log',
            'level': level,
            'message': msg,
            'time': datetime.now().strftime('%H:%M:%S')
        })
    
    try:
        log_callback(f"Запуск отправки в {len(groups)} групп...")
        
        suggester = VKSuggester(
            access_token=token,
            request_delay=delay,
            on_log=log_callback
        )
        
        # Проверяем токен
        try:
            user_info = suggester.get_user_info()
            log_callback(f"Авторизован как: {user_info.full_name}")
        except VKApiError as e:
            log_callback(f"Ошибка авторизации: {e.message}", "error")
            q.put({'type': 'complete', 'success': False, 'error': str(e)})
            return
        
        # Загружаем фото на VK (если есть)
        attachments = []
        if photos:
            log_callback(f"Загрузка {len(photos)} фото на VK...")
            for photo_file in photos:
                if active_tasks.get(task_id, {}).get('stop'):
                    log_callback("Остановлено пользователем", "warning")
                    break
                
                filepath = os.path.join(app.config['UPLOAD_FOLDER'], photo_file)
                if os.path.exists(filepath):
                    with open(filepath, 'rb') as f:
                        photo_data = f.read()
                    
                    attachment = suggester.upload_photo(photo_data, photo_file)
                    if attachment:
                        attachments.append(attachment)
                        log_callback(f"Фото загружено: {attachment}")
                    else:
                        log_callback(f"Не удалось загрузить фото: {photo_file}", "warning")
        
        attachments_str = ','.join(attachments) if attachments else None
        
        # Отправляем посты
        results = []
        total = len(groups)
        success_count = 0
        
        # Резолвим группы
        log_callback(f"Резолвинг {total} групп...")
        resolved = suggester.resolve_group_ids(groups)
        
        if not resolved:
            log_callback("Не удалось найти ни одной группы!", "error")
            q.put({'type': 'complete', 'success': False, 'error': 'Группы не найдены'})
            return
        
        log_callback(f"Найдено {len(resolved)} групп")
        
        # Получаем информацию
        groups_info = suggester.get_groups_info(list(resolved.values()))
        
        for i, (identifier, gid) in enumerate(resolved.items()):
            if active_tasks.get(task_id, {}).get('stop'):
                log_callback("Остановлено пользователем", "warning")
                break
            
            info = groups_info.get(gid)
            group_name = info.name if info else f"Группа {gid}"
            
            # Проверяем возможность отправки
            if info and not info.can_suggest and not info.can_post:
                log_callback(f"[{i+1}/{len(resolved)}] ✗ {group_name}: предложка закрыта", "warning")
                q.put({
                    'type': 'result',
                    'current': i + 1,
                    'total': len(resolved),
                    'group': group_name,
                    'status': 'suggest_disabled',
                    'success': False
                })
                continue
            
            # Отправляем
            result = suggester.post_to_suggestion(gid, group_name, message, attachments_str)
            results.append(result)
            
            if result.status == PostStatus.SUCCESS:
                success_count += 1
                log_callback(f"[{i+1}/{len(resolved)}] ✓ {group_name}: успешно отправлено")
                q.put({
                    'type': 'result',
                    'current': i + 1,
                    'total': len(resolved),
                    'group': group_name,
                    'status': 'success',
                    'success': True,
                    'post_id': result.post_id
                })
            else:
                error_msg = result.error_message or result.status.value
                log_callback(f"[{i+1}/{len(resolved)}] ✗ {group_name}: {error_msg}", "warning")
                q.put({
                    'type': 'result',
                    'current': i + 1,
                    'total': len(resolved),
                    'group': group_name,
                    'status': result.status.value,
                    'success': False,
                    'error': error_msg
                })
                
                # Прерываем при ошибке авторизации
                if result.status == PostStatus.AUTH_ERROR:
                    log_callback("Критическая ошибка авторизации! Требуется новый токен.", "error")
                    break
        
        # Итоги
        log_callback("=" * 50)
        log_callback(f"Завершено! Успешно: {success_count}/{len(resolved)}")
        
        q.put({
            'type': 'complete',
            'success': True,
            'total': len(resolved),
            'success_count': success_count,
            'failed_count': len(resolved) - success_count
        })
        
    except Exception as e:
        log_callback(f"Критическая ошибка: {str(e)}", "error")
        q.put({'type': 'complete', 'success': False, 'error': str(e)})
    
    finally:
        # Очищаем через некоторое время
        active_tasks[task_id]['status'] = 'completed'
        
        def cleanup():
            time.sleep(300)  # 5 минут
            log_queues.pop(task_id, None)
            active_tasks.pop(task_id, None)
        
        threading.Thread(target=cleanup, daemon=True).start()


if __name__ == '__main__':
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
    app.run(debug=True, host='0.0.0.0', port=5000, threaded=True)
