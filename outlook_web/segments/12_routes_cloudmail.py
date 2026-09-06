from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    # These segmented files are executed into the shared `web_outlook_app`
    # globals at runtime. Importing from the assembled module keeps IDE
    # inspections from flagging the shared names as unresolved.
    from web_outlook_app import *  # noqa: F403


# ==================== cloud-mail 临时邮箱（maillab/cloud-mail）====================
# cloud-mail 的开放接口全部挂在 /api 前缀下，鉴权头是 Authorization，值为
# genToken 换来的公开令牌（与登录 JWT 复用同一个头）：
#   POST /api/public/genToken  {email, password}            -> data: {token}
#   POST /api/public/addUser   {list: [{email, password}]}  -> data: null
#   POST /api/public/emailList {toEmail, num, size}         -> data: 邮件列表
# 返回体统一是信封 {code: 200, message: 'success', data: ...}，而且**HTTP 状态码
# 恒为 200**，业务失败只体现在信封的 code/message 上，因此不能只看状态码。
#
# 与 cloudflare_temp_email 渠道最大的区别：域名由 cloud-mail 自己的 env.domain
# 决定，管理侧只提供一个实例，所以这里用设置项而不是渠道表。

CLOUDMAIL_DEFAULT_API_PREFIX = '/api'
CLOUDMAIL_PAGE_SIZE = 50
# emailList 的 size 上限；再大会被服务端切回 50，所以单页最大只能到这个值。
CLOUDMAIL_MAX_PAGE_SIZE = 200
# offset 与页边界不对齐时，最多往上游走几页，宁可少给也不要无限翻页。
CLOUDMAIL_MAX_PAGES = 10


def get_cloudmail_base_url() -> str:
    return str(get_setting('cloudmail_base_url', '') or '').strip().rstrip('/')


def get_cloudmail_api_prefix() -> str:
    prefix = str(get_setting('cloudmail_api_prefix', CLOUDMAIL_DEFAULT_API_PREFIX) or '').strip()
    if not prefix:
        prefix = CLOUDMAIL_DEFAULT_API_PREFIX
    if not prefix.startswith('/'):
        prefix = '/' + prefix
    return prefix.rstrip('/') or CLOUDMAIL_DEFAULT_API_PREFIX


def get_cloudmail_admin_email() -> str:
    return str(get_setting('cloudmail_admin_email', '') or '').strip()


def get_cloudmail_admin_password() -> str:
    return str(get_setting_decrypted('cloudmail_admin_password', '') or '').strip()


def get_cloudmail_domain() -> str:
    return str(get_setting('cloudmail_domain', '') or '').strip().lower().lstrip('@').rstrip('.')


def is_cloudmail_enabled() -> bool:
    return str(get_setting('cloudmail_enabled', 'false') or '').strip().lower() == 'true'


def get_cloudmail_public_token(force_refresh: bool = False) -> Optional[str]:
    """取得 cloud-mail 开放接口令牌；缓存于设置表，过期或失效时强制重取。"""
    if not force_refresh:
        cached = str(get_setting_decrypted('cloudmail_public_token', '') or '').strip()
        if cached:
            return cached

    base_url = get_cloudmail_base_url()
    admin_email = get_cloudmail_admin_email()
    admin_password = get_cloudmail_admin_password()
    if not base_url:
        return None
    if not admin_email or not admin_password:
        logging.warning('cloud-mail 未配置管理员邮箱或密码，无法获取开放接口令牌')
        return None

    result = cloudmail_request(
        'POST',
        '/public/genToken',
        json_data={'email': admin_email, 'password': admin_password},
    )
    if not result.get('success'):
        logging.error('cloud-mail 获取令牌失败: %s', result.get('error', '未知错误'))
        return None

    data = result.get('data') or {}
    token = str((data or {}).get('token', '') or '').strip()
    if not token:
        logging.error('cloud-mail 令牌响应缺少 token 字段')
        return None

    set_setting_encrypted('cloudmail_public_token', token)
    return token


def cloudmail_request(method: str, endpoint: str, token: Optional[str] = None,
                      json_data: Optional[Dict] = None,
                      params: Optional[Dict] = None) -> Dict[str, Any]:
    """调用 cloud-mail 接口并拆掉它的响应信封。

    任何失败都返回 {'success': False, 'error': 可读原因}，绝不抛出，也绝不把令牌
    或密码写进日志与返回值。
    """
    base_url = get_cloudmail_base_url()
    if not base_url:
        return {'success': False, 'error': '未配置 cloud-mail 服务地址'}

    url = f"{base_url}{get_cloudmail_api_prefix()}{endpoint}"
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['Authorization'] = token

    try:
        if method.upper() == 'GET':
            response = requests.get(url, headers=headers, params=params, timeout=30)
        elif method.upper() == 'POST':
            response = requests.post(url, headers=headers, params=params, json=json_data, timeout=30)
        elif method.upper() == 'DELETE':
            response = requests.delete(url, headers=headers, params=params, timeout=30)
        else:
            return {'success': False, 'error': '不支持的请求方法'}
    except Exception as exc:  # 网络、DNS、TLS 问题都归到这里
        logging.error('cloud-mail 请求异常 %s: %s', url, exc)
        return {'success': False, 'error': f'请求 cloud-mail 失败: {exc}'}

    if response.status_code not in (200, 201):
        return {'success': False, 'error': f'cloud-mail HTTP {response.status_code}'}

    # cloud-mail 即使业务失败也回 HTTP 200，所以先看能不能解析成 JSON。
    # 把前端页面地址当成服务地址填进来时，这里收到的是一整页 HTML，
    # 提示必须直接说清这一点，否则排查成本极高。
    try:
        payload = response.json()
    except Exception:
        return {
            'success': False,
            'error': (
                'cloud-mail 返回的不是 JSON。请确认服务地址填的是接口地址'
                '（例如 https://mail-api.example.com），而不是前端页面域名'
            ),
        }

    if not isinstance(payload, dict):
        return {'success': False, 'error': 'cloud-mail 响应格式异常'}

    code = payload.get('code')
    if code not in (200, 0, None):
        return {'success': False, 'error': str(payload.get('message') or f'cloud-mail 返回 code={code}')}

    return {'success': True, 'data': payload.get('data')}


def cloudmail_create_address(username: Optional[str] = None,
                             domain: Optional[str] = None) -> Dict[str, Any]:
    """在 cloud-mail 上创建一个收件地址。

    返回 {'success': True, 'address': ..., 'password': ...}；密码只在这一层出现，
    由调用方加密入库，用于后续可能的登录态操作。
    """
    token = get_cloudmail_public_token()
    if not token:
        return {'success': False, 'error': '无法获取 cloud-mail 开放接口令牌，请检查服务地址与管理员账号'}

    resolved_domain = (domain or get_cloudmail_domain() or '').strip().lower()
    if not resolved_domain:
        return {'success': False, 'error': '请先在设置中配置 cloud-mail 收信域名'}

    local_part = (username or '').strip().lower() or secrets.token_hex(6)
    if not re.match(r'^[a-z0-9._+-]{3,64}$', local_part):
        return {'success': False, 'error': '用户名只允许字母、数字与 . _ + -，且至少 3 个字符'}

    password = secrets.token_urlsafe(12)
    email_addr = f'{local_part}@{resolved_domain}'

    result = cloudmail_request(
        'POST',
        '/public/addUser',
        token=token,
        json_data={'list': [{'email': email_addr, 'password': password}]},
    )
    if not result.get('success'):
        error_text = str(result.get('error') or '')
        if 'domain' in error_text.lower() or '域名' in error_text:
            error_text += '（请确认该域名在 cloud-mail 服务端的 domain 配置里）'
        return {'success': False, 'error': error_text or '创建 cloud-mail 地址失败'}

    return {'success': True, 'address': email_addr, 'password': password}


def cloudmail_normalize_messages(email_addr: str, rows: Any) -> List[Dict[str, Any]]:
    """把 cloud-mail 的邮件行转成本系统统一的临时邮件结构。"""
    messages: List[Dict[str, Any]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        raw_html = str(row.get('content') or '')
        raw_text = str(row.get('text') or '')
        has_html = raw_html.strip().lower().startswith('<')
        content = raw_text or (html.unescape(re.sub(r'<[^>]+>', ' ', raw_html)) if has_html else raw_html)
        messages.append({
            'id': str(row.get('emailId') or row.get('id') or ''),
            'from_address': str(row.get('sendEmail') or row.get('sendName') or '未知'),
            'subject': str(row.get('subject') or '无主题'),
            'content': content,
            'html_content': raw_html if has_html else '',
            'has_html': 1 if has_html else 0,
            'timestamp': _cloudmail_timestamp(row.get('createTime')),
        })
    return messages


def _cloudmail_timestamp(value: Any) -> int:
    """cloud-mail 的 createTime 是 'YYYY-MM-DD HH:MM:SS'，统一成秒级时间戳。"""
    text = str(value or '').strip()
    if not text:
        return 0
    if text.isdigit():
        return int(text)
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S'):
        try:
            return int(time.mktime(time.strptime(text[:19], fmt)))
        except ValueError:
            continue
    return 0


def _cloudmail_rows(payload: Any) -> List[Dict[str, Any]]:
    """cloud-mail 列表类接口有两种形状：直接是数组，或者 {list: [...], total: n}。"""
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        rows = payload.get('list') or payload.get('rows') or payload.get('emails') or []
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    return []


def cloudmail_list_messages(email_addr: str, num: int = 1,
                            size: int = CLOUDMAIL_PAGE_SIZE) -> Dict[str, Any]:
    token = get_cloudmail_public_token()
    if not token:
        return {'success': False, 'error': '无法获取 cloud-mail 开放接口令牌'}

    result = cloudmail_request(
        'POST',
        '/public/emailList',
        token=token,
        json_data={'toEmail': email_addr, 'num': max(1, int(num)), 'size': max(1, int(size))},
    )
    if not result.get('success'):
        return {'success': False, 'error': result.get('error', '获取 cloud-mail 邮件失败')}

    return {'success': True, 'rows': _cloudmail_rows(result.get('data'))}


def fetch_cloudmail_temp_messages(email_addr: str, temp_email: Optional[Dict[str, Any]],
                                  limit: int = CLOUDMAIL_PAGE_SIZE,
                                  offset: int = 0) -> Dict[str, Any]:
    """与 fetch_cloudflare_temp_messages 对齐的入口：拉取并统一某个地址的邮件。

    emailList 只有页码 num 与页大小 size，没有 offset：能整除时直接取那一页，
    否则从前往后逐页走到位（最多 MAX_PAGES 页），保证 offset 语义正确而不是直接报错。
    """
    wanted_limit = max(1, min(int(limit or CLOUDMAIL_PAGE_SIZE), CLOUDMAIL_MAX_PAGE_SIZE))
    offset = max(0, int(offset or 0))

    if offset % wanted_limit == 0:
        page = offset // wanted_limit + 1
        listed = cloudmail_list_messages(email_addr, num=page, size=wanted_limit)
        if not listed.get('success'):
            return {'success': False, 'error': listed.get('error', '获取 cloud-mail 邮件失败')}
        rows = listed.get('rows')
    else:
        # 页边界不对齐：逐页拼接再切片，页数上限防止无限往上游发请求。
        collected: List[Any] = []
        for page in range(1, CLOUDMAIL_MAX_PAGES + 1):
            listed = cloudmail_list_messages(email_addr, num=page, size=CLOUDMAIL_PAGE_SIZE)
            if not listed.get('success'):
                return {'success': False, 'error': listed.get('error', '获取 cloud-mail 邮件失败')}
            page_rows = listed.get('rows')
            collected.extend(page_rows)
            if len(page_rows) < CLOUDMAIL_PAGE_SIZE or len(collected) >= offset + wanted_limit:
                break
        rows = collected[offset:offset + wanted_limit] or collected

    return {
        'success': True,
        'messages': cloudmail_normalize_messages(email_addr, rows),
        'method': 'cloud-mail',
    }


def _cloudmail_is_auth_failure(error: Any) -> bool:
    text = str(error or '').lower()
    return 'token' in text or '身份认证' in text or 'unauthor' in text or '登录' in text


def get_cloudmail_admin_token(force_refresh: bool = False) -> Optional[str]:
    """cloud-mail 的管理员 JWT。

    开放接口令牌只能建号与查邮件，/user/list 与删除地址要登录态，所以单独缓存一份；
    它和开放接口令牌一样只以密文入库，也绝不回给接口调用方。
    """
    if not force_refresh:
        cached = str(get_setting_decrypted('cloudmail_admin_token', '') or '').strip()
        if cached:
            return cached

    admin_email = get_cloudmail_admin_email()
    admin_password = get_cloudmail_admin_password()
    if not admin_email or not admin_password:
        return None

    result = cloudmail_request('POST', '/login', json_data={'email': admin_email, 'password': admin_password})
    if not result.get('success'):
        logging.error('cloud-mail 管理员登录失败: %s', result.get('error', '未知错误'))
        return None

    token = str((result.get('data') or {}).get('token', '') or '').strip()
    if not token:
        return None
    set_setting_encrypted('cloudmail_admin_token', token)
    return token


def _cloudmail_admin_request(method: str, endpoint: str, params: Optional[Dict] = None,
                             json_data: Optional[Dict] = None) -> Dict[str, Any]:
    """带管理员登录态的请求；登录态失效时重登一次再试，不循环重试。"""
    for attempt in (0, 1):
        token = get_cloudmail_admin_token(force_refresh=attempt > 0)
        if not token:
            return {'success': False, 'error': '无法获取 cloud-mail 管理员登录态，请检查管理员邮箱与密码'}

        result = cloudmail_request(method, endpoint, token=token, params=params, json_data=json_data)
        if result.get('success'):
            return result
        if not _cloudmail_is_auth_failure(result.get('error')):
            return result
        set_setting_encrypted('cloudmail_admin_token', '')

    return result


def cloudmail_find_user_id(email_addr: str) -> Dict[str, Any]:
    """把邮箱地址解析成 cloud-mail 的 userId。

    /user/list 的 email 参数是 **前缀 LIKE** 而不是精确匹配，所以必须在本地再精确比一次；
    命中不唯一就直接拒绝，绝不能“猜一个 id”去删账号。
    """
    target = str(email_addr or '').strip().lower()
    if not target:
        return {'success': False, 'error': '邮箱地址为空'}

    listed = _cloudmail_admin_request('GET', '/user/list',
                                      params={'email': target, 'num': 1, 'size': CLOUDMAIL_PAGE_SIZE})
    if not listed.get('success'):
        return {'success': False, 'error': listed.get('error', '查询 cloud-mail 用户失败')}

    exact = [
        row for row in _cloudmail_rows(listed.get('data'))
        if str(row.get('email') or '').strip().lower() == target
    ]
    if not exact:
        return {'success': False, 'error': f'cloud-mail 上不存在 {target}'}
    if len(exact) > 1:
        return {'success': False, 'error': 'cloud-mail 返回多条完全同名的地址，已拒绝删除'}

    if target == get_cloudmail_admin_email().strip().lower():
        return {'success': False, 'error': '拒绝删除 cloud-mail 管理员账号本身'}

    user_id = exact[0].get('userId')
    if user_id in (None, ''):
        return {'success': False, 'error': 'cloud-mail 用户列表里没有 userId'}
    return {'success': True, 'user_id': int(user_id)}


def cloudmail_delete_address(email_addr: str) -> Dict[str, Any]:
    """删除 cloud-mail 上的地址（服务端是硬删除）。"""
    found = cloudmail_find_user_id(email_addr)
    if not found.get('success'):
        return found

    deleted = _cloudmail_admin_request('DELETE', '/user/delete', params={'userIds': str(found['user_id'])})
    if not deleted.get('success'):
        return {'success': False, 'error': deleted.get('error', '删除 cloud-mail 地址失败'),
                'user_id': found['user_id']}
    return {'success': True, 'user_id': found['user_id']}


def build_cloudmail_messages_response(messages: List[Dict[str, Any]],
                                      new_count: Optional[int] = None) -> Any:
    """统一成临时邮箱列表接口已有的响应形状。"""
    payload: Dict[str, Any] = {
        'success': True,
        'emails': [
            {
                'id': msg.get('id'),
                'from': msg.get('from_address', '未知'),
                'subject': msg.get('subject', '无主题'),
                'body_preview': (msg.get('content', '') or '')[:200],
                'date': msg.get('timestamp', 0),
                'timestamp': msg.get('timestamp', 0),
                'has_html': 1 if msg.get('has_html') else 0,
            }
            for msg in messages
        ],
        'count': len(messages),
        'method': 'cloud-mail',
    }
    if new_count is not None:
        payload['new_count'] = new_count
    return jsonify(payload)


def cloudmail_settings_payload() -> Dict[str, Any]:
    """给设置页用的视图：只回是否已配置，绝不回令牌与密码。"""
    return {
        'success': True,
        'enabled': is_cloudmail_enabled(),
        'base_url': get_cloudmail_base_url(),
        'api_prefix': get_cloudmail_api_prefix(),
        'admin_email': get_cloudmail_admin_email(),
        'admin_password_configured': bool(get_cloudmail_admin_password()),
        'public_token_configured': bool(get_setting_decrypted('cloudmail_public_token', '')),
        'domain': get_cloudmail_domain(),
    }


@app.route('/api/cloudmail/settings', methods=['GET'])
@login_required
def api_cloudmail_settings_get():
    return jsonify(cloudmail_settings_payload())


@app.route('/api/cloudmail/settings', methods=['POST'])
@login_required
def api_cloudmail_settings_save():
    data = request.json or {}

    if 'base_url' in data:
        base_url = str(data.get('base_url') or '').strip().rstrip('/')
        if base_url and not base_url.lower().startswith(('http://', 'https://')):
            return jsonify({'success': False, 'error': '服务地址必须以 http:// 或 https:// 开头'})
        set_setting('cloudmail_base_url', base_url)
        # 地址换了，两个旧令牌一定作废，清缓存避免拿旧凭据打新实例。
        set_setting_encrypted('cloudmail_public_token', '')
        set_setting_encrypted('cloudmail_admin_token', '')

    if 'api_prefix' in data:
        prefix = str(data.get('api_prefix') or '').strip() or CLOUDMAIL_DEFAULT_API_PREFIX
        set_setting('cloudmail_api_prefix', prefix if prefix.startswith('/') else '/' + prefix)

    if 'admin_email' in data:
        set_setting('cloudmail_admin_email', str(data.get('admin_email') or '').strip())

    # 只在收到非空值时覆盖密码与令牌，清空表单不应该把已存的凭据抹掉。
    if str(data.get('admin_password') or '').strip():
        set_setting_encrypted('cloudmail_admin_password', str(data['admin_password']).strip())
        set_setting_encrypted('cloudmail_public_token', '')
        set_setting_encrypted('cloudmail_admin_token', '')

    if 'domain' in data:
        set_setting('cloudmail_domain', str(data.get('domain') or '').strip().lower().lstrip('@').rstrip('.'))

    if 'enabled' in data:
        set_setting('cloudmail_enabled', 'true' if data.get('enabled') else 'false')

    return jsonify(cloudmail_settings_payload())


@app.route('/api/cloudmail/test', methods=['POST'])
@login_required
def api_cloudmail_test():
    """连通性自检：取一次令牌，再按给定地址拉一次邮件列表。"""
    data = request.json or {}
    token = get_cloudmail_public_token(force_refresh=True)
    if not token:
        return jsonify({
            'success': False,
            'error': '无法获取 cloud-mail 令牌：请检查服务地址、接口前缀与管理员账号密码',
        })

    email_addr = str(data.get('email') or '').strip()
    if email_addr:
        fetched = fetch_cloudmail_temp_messages(email_addr, None)
        if not fetched.get('success'):
            return jsonify({'success': False, 'error': fetched.get('error', '读取邮件失败')})
        return jsonify({'success': True, 'message': 'cloud-mail 连接正常', 'count': len(fetched['messages'])})

    return jsonify({'success': True, 'message': 'cloud-mail 令牌获取成功'})
