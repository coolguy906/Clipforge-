
--- a/clipforge.py
+++ b/clipforge.py
@@
-HOST = os.environ.get("CLIPFORGE_HOST", "127.0.0.1")
-PORT = int(os.environ.get("CLIPFORGE_PORT", "8787"))
+HOST = os.environ.get("CLIPFORGE_HOST", "0.0.0.0")
+PORT = int(os.environ.get("PORT", os.environ.get("CLIPFORGE_PORT", "8787")))
 PUBLIC_URL = os.environ.get("CLIPFORGE_PUBLIC_URL", "").rstrip("/")
+ALLOWED_ORIGINS = {
+    origin.strip().rstrip("/")
+    for origin in os.environ.get(
+        "CLIPFORGE_ALLOWED_ORIGINS",
+        "https://clipforge78.netlify.app"
+    ).split(",")
+    if origin.strip()
+}
@@
     def send_bytes(self, code, body, ctype, extra=None):
         self.send_response(code)
         self.send_header("Content-Type", ctype)
         self.send_header("Content-Length", str(len(body)))
         self.send_header("Cache-Control", "no-store")
+        origin = self.headers.get("Origin", "").rstrip("/")
+        if origin in ALLOWED_ORIGINS:
+            self.send_header("Access-Control-Allow-Origin", origin)
+            self.send_header("Vary", "Origin")
         for key, value in (extra or {}).items():
             self.send_header(key, value)
         self.end_headers()
         self.wfile.write(body)
@@
         self.send_header("Content-Length", str(length))
         self.send_header("Accept-Ranges", "bytes")
+        origin = self.headers.get("Origin", "").rstrip("/")
+        if origin in ALLOWED_ORIGINS:
+            self.send_header("Access-Control-Allow-Origin", origin)
+            self.send_header("Vary", "Origin")
         if code == 206:
             self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
@@
     def dispatch(self, method):
         parsed = urllib.parse.urlparse(self.path)
         path, query = parsed.path, urllib.parse.parse_qs(parsed.query)
+
+        origin = self.headers.get("Origin", "").rstrip("/")
+        if origin and origin not in ALLOWED_ORIGINS:
+            return self.send_error_json(403, "Origin not allowed.")
+
+        if method == "OPTIONS":
+            self.send_response(204)
+            if origin:
+                self.send_header("Access-Control-Allow-Origin", origin)
+                self.send_header("Vary", "Origin")
+            self.send_header(
+                "Access-Control-Allow-Methods",
+                "GET, POST, OPTIONS"
+            )
+            self.send_header(
+                "Access-Control-Allow-Headers",
+                "Content-Type, X-Filename"
+            )
+            self.send_header("Access-Control-Max-Age", "86400")
+            self.send_header("Content-Length", "0")
+            self.end_headers()
+            return
+
         try:
-            # Reached through a public tunnel? Only rendered clips may be fetched (Instagram needs that).
-            if not self.is_local():
-                if method == "GET" and re.fullmatch(r"/clips/\w+\.mp4", path):
-                    return self.send_clip(path)
-                return self.send_error_json(403, "Only rendered clips are shared publicly.")
             if method == "GET":
                 return self.route_get(path, query)
-            return self.route_post(path)
+            if method == "POST":
+                return self.route_post(path)
+            return self.send_error_json(405, "Method not allowed.")
         except BadRequest as exc:
             self.send_error_json(400, str(exc))
         except (BrokenPipeError, ConnectionResetError):
             pass
         except Exception as exc:
             self.send_error_json(500, str(exc)[:500])
@@
     def do_POST(self):
         self.dispatch("POST")
+
+    def do_OPTIONS(self):
+        self.dispatch("OPTIONS")
