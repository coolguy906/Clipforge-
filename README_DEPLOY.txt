ClipForge deployment update

Replace these files in the ROOT of the GitHub repository:
- clipforge.py
- index.html
- requirements.txt
- Dockerfile

Then commit and push. Render must deploy using the Dockerfile. This rebuild installs
current yt-dlp[default] and Deno, which yt-dlp can use for YouTube JavaScript
challenge solving. The backend calls yt-dlp with --js-runtimes deno.

After deployment, check /api/status and confirm ytdlp is true. If YouTube still
fails after this update, inspect Render logs: YouTube may be rejecting Render's
outbound IP or requiring a challenge/token. Updating yt-dlp cannot bypass an
IP block or video access restriction.


INSTAGRAM REELS PUBLISHING SETUP

In Render > your service > Environment, configure these variables (never paste
the access token into GitHub or share it in screenshots):
- INSTAGRAM_USER_ID: Instagram professional account ID for the API login flow you use.
- INSTAGRAM_ACCESS_TOKEN: valid access token with the matching content-publishing permission.
- CLIPFORGE_PUBLIC_URL: https://clipforge-9-ulz9.onrender.com (use your actual Render URL, NOT the Netlify URL).
- GRAPH_BASE_URL: optional. Leave unset for Facebook Login (default graph.facebook.com).
  If your app uses Instagram Login, set the Graph host required by that flow; verify
  the exact host and supported API version in Meta's current docs for your app.
- INSTAGRAM_GRAPH_VERSION: optional version segment for the default Facebook Graph host.

The Instagram account must be a professional Business or Creator account, and the
Meta app/token must have the correct publishing permission for its chosen login flow.
The rendered clip URL must open publicly over HTTPS without cookies or sign-in, because
Meta fetches the MP4 from that URL. Test a rendered /clips/<id>.mp4 link in a private
browser window before attempting to publish. The API creates a Reel container, waits
for status FINISHED, then calls media_publish. Errors now include Meta's message/code.

Deploying this code does not automatically connect Instagram: valid Meta app
configuration, account ID, access token, permissions and a public video URL are required.


YOUTUBE NIGHTLY BUILD NOTE
This Dockerfile installs yt-dlp's nightly PyPI build with --pre, plus the
yt-dlp[default] EJS scripts and Deno runtime. The Docker build prints `yt-dlp --version`
so the Render build log proves which version was installed. If the same extraction
error remains on the latest nightly, capture the full yt-dlp verbose output: this may
be a YouTube IP/network block on the Render host, which a package update cannot fix.
