"""Password login and revocable, durable browser sessions for a single trusted workspace."""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import tempfile
import threading
import time
from contextlib import closing
from http.cookies import SimpleCookie, CookieError
from pathlib import Path

COOKIE = 'omnitrix_session'


class LoginError(ValueError):
    def __init__(self, message='Invalid username or password', status=401):
        super().__init__(message)
        self.status = status


def password_hash(password):
    if not isinstance(password, str) or not 12 <= len(password) <= 1024:
        raise ValueError('Use a password of 12–1024 characters')
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1, maxmem=67108864)
    return 'scrypt$' + base64.b64encode(salt).decode() + '$' + base64.b64encode(digest).decode()


def verify_password(password, encoded):
    try:
        if not isinstance(encoded, str):
            return False
        kind, salt, expected = encoded.split('$')
        if kind != 'scrypt' or not isinstance(password, str) or len(password) > 1024:
            return False
        result = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt, validate=True), n=16384, r=8, p=1, maxmem=67108864)
        return hmac.compare_digest(result, base64.b64decode(expected, validate=True))
    except (ValueError, TypeError):
        return False


def create_user(path, username, password):
    """Local operator bootstrap/reset; no public account creation endpoint."""
    if not re.fullmatch(r'[A-Za-z0-9_.@-]{1,100}', username):
        raise ValueError('Use a username with letters, digits, dot, @, dash or underscore')
    encoded = password_hash(password)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = json.loads(path.read_text()) if path.exists() else {'version': 1, 'users': {}}
    data['users'][username] = encoded
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as output:
            json.dump(data, output)
            output.write('\n')
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class Sessions:
    def __init__(self, users, database, secure=False, lifetime=8 * 3600):
        self.users, self.database = Path(users), Path(database)
        self.secure, self.lifetime = secure, lifetime
        if not self._users():
            raise ValueError('Create a dashboard user before enabling password login')
        self.database.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.database, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
        with closing(self._connect()) as db:
            db.executescript('''PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS sessions (digest TEXT PRIMARY KEY, username TEXT NOT NULL,
              credential TEXT NOT NULL, expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS login_attempts (ip TEXT NOT NULL, created REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS login_ip ON login_attempts(ip,created);''')
        self.dummy = password_hash(secrets.token_urlsafe(24))
        self.slots = threading.BoundedSemaphore(4)

    def _users(self):
        data = json.loads(self.users.read_text())
        if data.get('version') != 1 or not isinstance(data.get('users'), dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in data['users'].items()):
            raise ValueError('Invalid dashboard user configuration')
        return data['users']

    def _connect(self):
        return sqlite3.connect(self.database, timeout=2)

    @staticmethod
    def _digest(value):
        return hashlib.sha256(value.encode()).hexdigest()

    @staticmethod
    def _csrf(token):
        return hmac.new(token.encode(), b'omnitrix-csrf', hashlib.sha256).hexdigest()

    def login(self, username, password, ip):
        if not isinstance(username, str) or len(username) > 100 or not isinstance(password, str):
            raise LoginError()
        if not self.slots.acquire(blocking=False):
            raise LoginError('Login busy; try again shortly', 429)
        try:
            now = time.time()
            with closing(self._connect()) as db, db:
                db.execute('DELETE FROM login_attempts WHERE created<?', (now - 300,))
                if db.execute('SELECT count(*) FROM login_attempts WHERE ip=?', (ip,)).fetchone()[0] >= 10:
                    raise LoginError('Too many login attempts; wait five minutes', 429)
                db.execute('INSERT INTO login_attempts VALUES(?,?)', (ip, now))
            users = self._users()
            encoded = users.get(username, self.dummy)
            valid = verify_password(password, encoded)
            if not valid or username not in users:
                raise LoginError()
            token, expires = secrets.token_urlsafe(32), now + self.lifetime
            with closing(self._connect()) as db, db:
                db.execute('DELETE FROM login_attempts WHERE ip=?', (ip,))
                db.execute('DELETE FROM sessions WHERE expires<?', (now,))
                db.execute('INSERT INTO sessions VALUES(?,?,?,?)', (self._digest(token), username, self._digest(encoded), expires))
            return token, {'username': username, 'csrf_token': self._csrf(token), 'expires_at': expires}
        finally:
            self.slots.release()

    def authenticate(self, cookie):
        jar = SimpleCookie()
        try:
            jar.load(cookie or '')
            token = jar[COOKIE].value
        except (KeyError, ValueError, CookieError):
            return None
        with closing(self._connect()) as db:
            row = db.execute('SELECT username,credential,expires FROM sessions WHERE digest=?', (self._digest(token),)).fetchone()
        if not row or row[2] <= time.time():
            return None
        encoded = self._users().get(row[0])
        if not encoded or not hmac.compare_digest(row[1], self._digest(encoded)):
            return None
        return {'mode': 'session', 'username': row[0], 'csrf_token': self._csrf(token), 'expires_at': row[2]}

    def logout(self, cookie):
        jar = SimpleCookie()
        jar.load(cookie or '')
        if COOKIE in jar:
            with closing(self._connect()) as db, db:
                db.execute('DELETE FROM sessions WHERE digest=?', (self._digest(jar[COOKIE].value),))

    def cookie(self, token='', clear=False):
        jar = SimpleCookie()
        jar[COOKIE] = token
        item = jar[COOKIE]
        item['path'], item['httponly'], item['samesite'] = '/', True, 'Strict'
        item['max-age'] = 0 if clear else self.lifetime
        if self.secure:
            item['secure'] = True
        return item.OutputString()
