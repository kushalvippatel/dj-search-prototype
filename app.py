from flask import Flask, render_template, request, jsonify
import requests
from bs4 import BeautifulSoup
import os
import re
import socket
import ipaddress
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse
import time as time_module

app = Flask(__name__)
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0

# iTunes storefront country codes we support (ISO 3166-1 alpha-2)
ITUNES_COUNTRIES = {
    'US', 'GB', 'DE', 'FR', 'IT', 'ES', 'CA', 'JP', 'AU', 'BR',
    'MX', 'IN', 'NL', 'SE', 'PL', 'SG', 'TR', 'AE', 'SA',
}

# Default headers for requests
DEFAULT_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.5',
    'Accept-Encoding': 'gzip, deflate',
    'DNT': '1',
    'Connection': 'keep-alive',
    'Upgrade-Insecure-Requests': '1'
}

# Session for connection pooling
session = requests.Session()
session.headers.update(DEFAULT_HEADERS)

# Domains the server is allowed to fetch on behalf of a client. Requests to
# anything else (internal hosts, cloud metadata endpoints, arbitrary sites)
# are rejected to prevent Server-Side Request Forgery (SSRF).
ALLOWED_SOURCE_DOMAINS = {
    'soundcloud.com', 'youtube.com', 'youtu.be', 'spotify.com',
}


def _resolves_to_public_ip(hostname):
    """Return True only if every address the hostname resolves to is a
    globally routable (public) IP. Blocks loopback/private/link-local/reserved
    ranges so an allowed-looking host can't be pointed at internal services."""
    try:
        addr_infos = socket.getaddrinfo(hostname, None)
    except (socket.gaierror, UnicodeError):
        return False

    for info in addr_infos:
        ip_str = info[4][0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            return False
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return False
    return True


def is_allowed_url(url, allowed_domains):
    """Validate a client-supplied URL before the server fetches it.

    Requires http(s), a host in the allowlist (exact match or subdomain), and
    a hostname that resolves only to public IPs."""
    if not url:
        return False
    try:
        parsed = urlparse(url)
    except ValueError:
        return False

    if parsed.scheme not in ('http', 'https'):
        return False

    host = parsed.hostname
    if not host:
        return False
    host = host.lower()

    domain_ok = any(host == d or host.endswith('.' + d) for d in allowed_domains)
    if not domain_ok:
        return False

    return _resolves_to_public_ip(host)

# Storefront country detection
def detect_country(request_obj):
    """Detect the user's iTunes storefront country from Accept-Language."""
    accept_language = request_obj.headers.get('Accept-Language', '')

    # Map common language codes to countries
    lang_to_country = {
        'en-US': 'US', 'en-GB': 'GB', 'en-CA': 'CA', 'en-AU': 'AU',
        'de': 'DE', 'de-DE': 'DE',
        'fr': 'FR', 'fr-FR': 'FR',
        'it': 'IT', 'it-IT': 'IT',
        'es': 'ES', 'es-ES': 'ES',
        'ja': 'JP', 'ja-JP': 'JP',
        'pt-BR': 'BR', 'pt': 'BR',
        'nl': 'NL', 'nl-NL': 'NL',
        'sv': 'SE', 'sv-SE': 'SE',
        'pl': 'PL', 'pl-PL': 'PL',
        'tr': 'TR', 'tr-TR': 'TR',
        'ar': 'AE', 'ar-AE': 'AE', 'ar-SA': 'SA'
    }

    for lang, country in lang_to_country.items():
        if lang in accept_language:
            return country

    # Default to US
    return 'US'


# Noise commonly appended to video titles that pollutes store searches,
# e.g. "(Official Video)", "[4K Remaster]", "(Lyric Video)".
TITLE_NOISE_RE = re.compile(
    r'[(\[][^()\[\]]*\b(official|video|audio|lyric|lyrics|visuali[sz]er|'
    r'remaster(ed)?|hd|hq|4k|mv|music video|out now|premiere|free (dl|download))\b[^()\[\]]*[)\]]',
    re.IGNORECASE,
)


def clean_track_query(artist, title):
    """Build a store search query from scraped artist/title.

    Video titles usually embed the artist ("Rick Astley - Never Gonna ...")
    and marketing noise ("(Official Video)"); naive "artist + title"
    concatenation duplicates the artist and rarely matches store catalogs."""
    artist = (artist or '').strip()
    title = (title or '').strip()

    title = TITLE_NOISE_RE.sub('', title).strip()

    # Drop a leading "Artist -" / "Artist:" prefix duplicated in the title
    if artist and title.lower().startswith(artist.lower()):
        rest = title[len(artist):].lstrip()
        if rest[:1] in ('-', ':', '|', '–', '—'):
            title = rest[1:].strip()
        elif rest and rest != title:
            title = rest

    # Collapse leftover whitespace/separators
    title = re.sub(r'\s{2,}', ' ', title).strip(' -–—|')

    return f"{artist} {title}".strip()

# Request helper with retry and timeout
def make_request(url, headers=None, timeout=10, max_retries=3, retry_delay=1):
    """Make HTTP request with retry logic and timeout."""
    if headers is None:
        headers = DEFAULT_HEADERS.copy()
    
    for attempt in range(max_retries):
        try:
            response = session.get(url, headers=headers, timeout=timeout)
            response.raise_for_status()
            return response
        except requests.exceptions.RequestException as e:
            if attempt < max_retries - 1:
                time_module.sleep(retry_delay * (2 ** attempt))  # Exponential backoff
                continue
            raise e
    return None

def get_soundcloud_data(url):
    try:
        response = make_request(url)
        if not response:
            return {'error': 'Failed to fetch SoundCloud data'}
        
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Get title and artist from metadata
        title = soup.find('meta', property='og:title')['content']
        
        # Get artist name and clean it up
        artist = 'Unknown Artist'
        artist_meta = soup.find('meta', property='soundcloud:user')
        if artist_meta:
            artist = artist_meta['content']
            # Remove any URL part if present
            if 'soundcloud.com/' in artist:
                artist = artist.split('soundcloud.com/')[-1]
        
        # Look for download/purchase links with more variations
        download_url = None
        download_keywords = ['free download', 'download', 'dl', 'buy', 'purchase', 'get it']
        
        for link in soup.find_all('a', href=True):
            href = link['href'].lower()
            link_text = link.get_text().lower()
            
            # Check both the URL and the link text for download keywords
            if any(keyword in href or keyword in link_text for keyword in download_keywords):
                download_url = link['href']
                break
        
        result = {
            'title': title,
            'artist': artist,
            'download_url': download_url
        }
        
        return result
            
    except Exception as e:
        return {'error': str(e)}

def build_bandcamp_embed(item_type, item_id):
    """Build Bandcamp player iframe HTML from a numeric ID we obtained from
    Bandcamp's own search API. The ID is validated as an integer, so no
    scraped markup ever reaches the embed."""
    item_id = int(item_id)
    kind = 'album' if item_type == 'a' else 'track'
    embed_url = (
        f"https://bandcamp.com/EmbeddedPlayer/{kind}={item_id}"
        f"/size=large/bgcol=ffffff/linkcol=0687f5/tracklist=false"
        f"{'' if kind == 'album' else '/artwork=small'}/transparent=true/"
    )
    return f'<iframe style="border: 0; width: 100%; height: 120px;" src="{embed_url}" seamless></iframe>'


def search_bandcamp(artist, title):
    """Search Bandcamp via its public autocomplete JSON API.

    The old HTML scrape broke when Bandcamp moved search results to
    client-side rendering; the JSON endpoint returns structured results
    including the numeric IDs needed for player embeds."""
    search_query = clean_track_query(artist, title)
    if not search_query:
        return {'search_url': '', 'tracks': []}

    search_url = f"https://bandcamp.com/search?q={requests.utils.quote(search_query)}"

    try:
        response = session.post(
            'https://bandcamp.com/api/bcsearch_public_api/1/autocomplete_elastic',
            json={
                'search_text': search_query,
                'search_filter': 't',  # tracks
                'full_page': False,
                'fan_id': None,
            },
            timeout=10,
        )
        response.raise_for_status()
        results = (response.json().get('auto') or {}).get('results') or []

        tracks = []
        for item in results:
            item_type = item.get('type')
            item_id = item.get('id')
            item_url = item.get('item_url_path')
            if item_type not in ('t', 'a') or not item_id or not item_url:
                continue

            band = (item.get('band_name') or '').strip()
            name = (item.get('name') or '').strip()
            try:
                embed_html = build_bandcamp_embed(item_type, item_id)
            except (TypeError, ValueError):
                embed_html = None

            tracks.append({
                'url': item_url,
                'embed_html': embed_html,
                'title': f"{band} - {name}".strip(' -'),
            })
            if len(tracks) >= 10:
                break

        return {'search_url': search_url, 'tracks': tracks}
    except Exception as e:
        print(f"Bandcamp search error: {str(e)}")
        return {'search_url': search_url, 'tracks': []}

def search_itunes(artist, title, country='US'):
    """Search the iTunes Store via Apple's public Search API.

    Replaces the old Amazon HTML scraping, which Amazon's bot detection
    reduced to empty shells. The iTunes API is free, keyless, and returns
    prices, artwork, store links, and 30-second audio previews."""
    if country not in ITUNES_COUNTRIES:
        country = 'US'

    search_query = clean_track_query(artist, title)
    if not search_query:
        return {'search_url': '', 'products': [], 'country': country}

    search_url = (
        f"https://music.apple.com/{country.lower()}/search?term="
        f"{requests.utils.quote(search_query)}"
    )

    try:
        response = session.get(
            'https://itunes.apple.com/search',
            params={
                'term': search_query,
                'media': 'music',
                'entity': 'song',
                'limit': 5,
                'country': country,
            },
            timeout=10,
        )
        response.raise_for_status()
        results = response.json().get('results') or []

        products = []
        for item in results:
            track_name = (item.get('trackName') or '').strip()
            artist_name = (item.get('artistName') or '').strip()
            track_url = item.get('trackViewUrl')
            if not track_name or not track_url:
                continue

            # trackPrice can be missing, or negative for album-only tracks
            price = item.get('trackPrice')
            currency = item.get('currency') or ''
            price_text = f"{price:.2f} {currency}".strip() if isinstance(price, (int, float)) and price >= 0 else None

            products.append({
                'title': f"{artist_name} - {track_name}".strip(' -'),
                'url': track_url,
                'price': price_text,
                'image': item.get('artworkUrl100'),
                'preview_audio': item.get('previewUrl'),
                'album': item.get('collectionName'),
            })

        return {'search_url': search_url, 'products': products, 'country': country}
    except Exception as e:
        print(f"iTunes search error: {str(e)}")
        return {'search_url': search_url, 'products': [], 'country': country}

def get_youtube_data(url):
    try:
        response = make_request(url)
        if not response:
            return {'error': 'Failed to fetch YouTube data'}
        
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Get title from metadata
        title = soup.find('meta', property='og:title')['content']
        
        # Initialize artist variable
        artist = None
        
        # Try different methods to get the channel name (artist)
        channel_tag = soup.find('link', itemprop='name')
        if channel_tag:
            artist = channel_tag['content']
        
        if not artist:
            video_tag = soup.find('meta', property='og:video:tag')
            if video_tag:
                artist = video_tag['content']
        
        # If we still don't have an artist, use a default
        if not artist:
            artist = "Unknown Artist"
        
        # Look for any purchase/download links in description
        download_url = None
        description = soup.find('meta', property='og:description')
        if description:
            desc_text = description['content'].lower()
            links = soup.find_all('a', href=True)
            for link in links:
                href = link['href'].lower()
                if any(keyword in href for keyword in ['bandcamp.com', 'buy', 'purchase', 'download']):
                    download_url = link['href']
                    break
        
        result = {
            'title': title,
            'artist': artist,
            'download_url': download_url
        }
        
        return result
            
    except Exception as e:
        print(f"YouTube scraping error: {str(e)}")
        return {'error': str(e)}

def get_spotify_data(url):
    try:
        response = make_request(url)
        if not response:
            return {'error': 'Failed to fetch Spotify data'}
        
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Find the h1 tag for the title
        h1_tag = soup.find('h1')
        if h1_tag:
            title = h1_tag.text.strip()
        else:
            title = None
            
        # Find the artist name
        artist_tag = soup.find('div', string='Artist')
        if artist_tag and artist_tag.find_next():
            artist = artist_tag.find_next().text.strip()
        else:
            artist = None
            
        if not title or not artist:
            return {'error': 'Could not extract track information'}
            
        result = {
            'title': title,
            'artist': artist,
            'download_url': None
        }
        
        return result
            
    except Exception as e:
        print(f"Spotify scraping error: {str(e)}")
        return {'error': str(e)}

def search_soundcloud(keywords):
    try:
        # Get the search URL
        search_query = keywords.replace(' ', '%20')
        search_url = f"https://soundcloud.com/search?q={search_query}"
        
        response = make_request(search_url)
        if not response:
            return None
        
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Find all track links
        tracks = []
        for link in soup.find_all('a', href=True):
            href = link['href']
            if '/tracks/' in href or len(href.split('/')) == 3:  # Track URL pattern
                track_url = f"https://soundcloud.com{href}" if href.startswith('/') else href
                # Get the oEmbed data for this track
                try:
                    oembed_url = f"https://soundcloud.com/oembed?url={track_url}&format=json"
                    oembed_response = make_request(oembed_url)
                    if oembed_response:
                        oembed_data = oembed_response.json()
                    else:
                        continue
                    tracks.append({
                        'url': track_url,
                        'embed_html': oembed_data['html']
                    })
                except:
                    continue
        
        result = {
            'search_url': search_url,
            'tracks': tracks[:5]  # Return first 5 tracks with embed code
        }
        
        return result
            
    except Exception as e:
        print(f"SoundCloud search error: {str(e)}")
        return None

def get_track_info(track_url):
    try:
        response = make_request(track_url)
        if not response:
            return {'error': 'Failed to fetch track info'}
        
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Look for download links
        download_url = None
        for link in soup.find_all('a', href=True):
            href = link['href'].lower()
            link_text = link.get_text().lower()
            if any(word in href or word in link_text for word in ['download', 'free dl', 'buy', 'purchase']):
                download_url = link['href']
                break
                
        result = {
            'download_url': download_url
        }
        
        return result
    except Exception as e:
        print(f"Error checking track: {str(e)}")
        return {'error': str(e)}

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/get-country', methods=['GET'])
def get_country():
    """Get the detected iTunes storefront country for the user."""
    return jsonify({'country': detect_country(request)})

@app.route('/search', methods=['POST'])
def search():
    url = request.json.get('url')
    country = request.json.get('country', 'US')

    if not url:
        return jsonify({'error': 'No URL provided'})

    if not is_allowed_url(url, ALLOWED_SOURCE_DOMAINS):
        return jsonify({'error': 'Unsupported or disallowed URL. Use a SoundCloud, YouTube, or Spotify link.'})

    host = (urlparse(url).hostname or '').lower()

    # Use concurrent execution for better performance
    with ThreadPoolExecutor(max_workers=3) as executor:
        # Determine the source URL type and fetch data
        if host == 'youtu.be' or host.endswith('youtube.com'):
            data_future = executor.submit(get_youtube_data, url)
            source = 'YouTube'
        elif host.endswith('spotify.com'):
            data_future = executor.submit(get_spotify_data, url)
            source = 'Spotify'
        else:
            data_future = executor.submit(get_soundcloud_data, url)
            source = 'SoundCloud'
        
        # Fetch Bandcamp and iTunes data concurrently
        data = data_future.result()

        if 'error' in data:
            return jsonify(data)

        bandcamp_future = executor.submit(search_bandcamp, data['artist'], data['title'])
        itunes_future = executor.submit(search_itunes, data['artist'], data['title'], country)

        bandcamp_result = bandcamp_future.result()
        itunes_result = itunes_future.result()

    return jsonify({
        'source': source,
        'download_url': data.get('download_url'),
        'bandcamp': bandcamp_result,
        'itunes': itunes_result,
        'title': data['title'],
        'artist': data['artist']
    })

# Add a new route for keyword search
@app.route('/keyword-search', methods=['POST'])
def keyword_search():
    keywords = request.json.get('keywords')
    country = request.json.get('country', 'US')

    if not keywords:
        return jsonify({'error': 'No keywords provided'})

    # Use concurrent execution for better performance
    with ThreadPoolExecutor(max_workers=3) as executor:
        soundcloud_future = executor.submit(search_soundcloud, keywords)
        bandcamp_future = executor.submit(search_bandcamp, keywords, '')
        itunes_future = executor.submit(search_itunes, keywords, '', country)

        soundcloud_results = soundcloud_future.result()
        bandcamp_result = bandcamp_future.result()
        itunes_result = itunes_future.result()

    return jsonify({
        'soundcloud': soundcloud_results if soundcloud_results else None,
        'bandcamp': bandcamp_result,
        'itunes': itunes_result
    })

@app.route('/check-track', methods=['POST'])
def check_track():
    track_url = request.json.get('track_url')
    if not track_url:
        return jsonify({'error': 'No track URL provided'})

    if not is_allowed_url(track_url, ALLOWED_SOURCE_DOMAINS):
        return jsonify({'error': 'Disallowed track URL'})

    track_info = get_track_info(track_url)
    return jsonify(track_info)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)