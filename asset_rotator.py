import os
import time
import secrets
import json
import re
ALLOWED_API_IPS = {
    "78.154.103.27",
    "127.0.0.1",
    "localhost"
}
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".web_cache")
os.makedirs(CACHE_DIR, exist_ok=True)
class DynamicAssetRotator:
    def __init__(self):
        self.current_token = ""
        self.current_js_filename = ""
        self.current_html_content = ""
        self.current_js_content = ""
        self.last_html_mtime = 0
        self.last_js_mtime = 0
        self.rotate_assets()
    def get_client_ip(self, request):
        headers = request.headers
        for h in ["CF-Connecting-IP", "X-Real-IP", "X-Forwarded-For"]:
            ip_val = headers.get(h)
            if ip_val:
                return ip_val.split(",")[0].strip()
        return request.remote or ""
    def is_ip_allowed(self, request):
        return True
    def obfuscate_js(self, raw_js: str) -> str:
        anti_tamper = """
(function(){
    document.addEventListener('contextmenu', function(e){ e.preventDefault(); return false; });
    document.addEventListener('keydown', function(e){
        if(e.keyCode == 123 || (e.ctrlKey && e.shiftKey && (e.keyCode == 73 || e.keyCode == 74)) || (e.ctrlKey && e.keyCode == 85)){
            e.preventDefault();
            return false;
        }
    });
})();
"""
        import base64
        full_code = anti_tamper + "\n" + raw_js
        encoded = base64.b64encode(full_code.encode("utf-8")).decode("utf-8")
        rot_wrapper = f"""(function(){{
    try {{
        var bin = atob("{encoded}");
        var len = bin.length;
        var bytes = new Uint8Array(len);
        for (var i = 0; i < len; i++) {{
            bytes[i] = bin.charCodeAt(i);
        }}
        var code = new TextDecoder("utf-8").decode(bytes);
        window.eval(code);
    }} catch (e) {{
        console.error("Engine loader error:", e);
    }}
}})();"""
        return rot_wrapper.strip()
    def load_source_files(self):
        base_dir = os.path.dirname(os.path.abspath(__file__))
        html_file = os.path.join(base_dir, "2minesg.html")
        js_file = os.path.join(base_dir, "indexapp_mines.js")
        raw_html = ""
        raw_js = ""
        if os.path.isfile(html_file):
            self.last_html_mtime = os.path.getmtime(html_file)
            with open(html_file, "r", encoding="utf-8") as f:
                raw_html = f.read()
        if os.path.isfile(js_file):
            self.last_js_mtime = os.path.getmtime(js_file)
            with open(js_file, "r", encoding="utf-8") as f:
                raw_js = f.read()
        return raw_html, raw_js
    def check_and_reload(self):
        base_dir = os.path.dirname(os.path.abspath(__file__))
        html_file = os.path.join(base_dir, "2minesg.html")
        js_file = os.path.join(base_dir, "indexapp_mines.js")
        mtime_h = os.path.getmtime(html_file) if os.path.isfile(html_file) else 0
        mtime_j = os.path.getmtime(js_file) if os.path.isfile(js_file) else 0
        if mtime_h != self.last_html_mtime or mtime_j != self.last_js_mtime or not self.current_html_content:
            self.rotate_assets()
    def rotate_assets(self):
        raw_html, raw_js = self.load_source_files()
        if not raw_html or not raw_js:
            return
        new_token = secrets.token_hex(6)
        new_js_filename = f"indexapp_{new_token}.js"
        obfuscated_js = self.obfuscate_js(raw_js)
        updated_html = re.sub(
            r'<script\s+src="[^"]*indexapp[^"]*\.js"><\/script>',
            f'<script src="/{new_js_filename}"></script>',
            raw_html,
            flags=re.IGNORECASE
        )
        if f'/{new_js_filename}' not in updated_html:
            updated_html = updated_html.replace('</body>', f'<script src="/{new_js_filename}"></script>\n</body>')
        try:
            for fname in os.listdir(CACHE_DIR):
                if fname.startswith("indexapp_") and fname.endswith(".js"):
                    try:
                        os.remove(os.path.join(CACHE_DIR, fname))
                    except Exception:
                        pass
        except Exception:
            pass
        new_js_path = os.path.join(CACHE_DIR, new_js_filename)
        with open(new_js_path, "w", encoding="utf-8") as f:
            f.write(obfuscated_js)
        self.current_token = new_token
        self.current_js_filename = new_js_filename
        self.current_html_content = updated_html
        self.current_js_content = obfuscated_js
        self.last_rotation = time.time()
asset_rotator = DynamicAssetRotator()