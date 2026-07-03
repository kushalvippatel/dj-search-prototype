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

# Amazon store mapping
AMAZON_STORES = {
    'US': 'amazon.com',
    'UK': 'amazon.co.uk',
    'DE': 'amazon.de',
    'FR': 'amazon.fr',
    'IT': 'amazon.it',
    'ES': 'amazon.es',
    'CA': 'amazon.ca',
    'JP': 'amazon.co.jp',
    'AU': 'amazon.com.au',
    'BR': 'amazon.com.br',
    'MX': 'amazon.com.mx',
    'IN': 'amazon.in',
    'NL': 'amazon.nl',
    'SE': 'amazon.se',
    'PL': 'amazon.pl',
    'SG': 'amazon.sg',
    'TR': 'amazon.com.tr',
    'AE': 'amazon.ae',
    'SA': 'amazon.sa'
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
ALLOWED_BANDCAMP_DOMAINS = {'bandcamp.com'}


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

# Amazon location detection
def detect_amazon_store(request_obj):
    """Detect Amazon store based on request headers."""
    # Try to get country from Accept-Language header
    accept_language = request_obj.headers.get('Accept-Language', '')
    
    # Map common language codes to countries
    lang_to_country = {
        'en-US': 'US', 'en-GB': 'UK', 'en-CA': 'CA', 'en-AU': 'AU',
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
    
    # Check Accept-Language
    for lang, country in lang_to_country.items():
        if lang in accept_language:
            return AMAZON_STORES.get(country, 'amazon.com')
    
    # Default to US
    return 'amazon.com'

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

def search_bandcamp(artist, title):
    search_query = f"{artist} {title}".strip()
    if not search_query:
        return {'search_url': '', 'tracks': []}
    
    search_url = f"https://bandcamp.com/search?q={requests.utils.quote(search_query)}"
    
    try:
        response = make_request(search_url)
        if not response:
            print(f"Bandcamp search: Failed to fetch {search_url}")
            return {'search_url': search_url, 'tracks': []}
        
        soup = BeautifulSoup(response.text, 'html.parser')
        tracks = []
        seen_urls = set()
        
        # Find track/album links in search results - try multiple methods
        all_links = soup.find_all('a', href=True)
        
        print(f"Bandcamp search: Found {len(all_links)} total links")
        
        for link in all_links:
            href = link.get('href', '')
            if not href:
                continue
            
            # Check for Bandcamp track/album URLs - be more flexible
            is_bandcamp_url = False
            if '/album/' in href or '/track/' in href:
                is_bandcamp_url = True
            elif '.bandcamp.com' in href and ('album' in href or 'track' in href):
                is_bandcamp_url = True
            
            if not is_bandcamp_url:
                continue
            
            # Handle both relative and absolute URLs
            if href.startswith('/'):
                full_url = f"https://bandcamp.com{href}"
            elif href.startswith('http'):
                full_url = href
            elif '.bandcamp.com' in href:
                full_url = f"https://{href}" if not href.startswith('http') else href
            else:
                full_url = f"https://bandcamp.com/{href}"
            
            # Normalize URL (remove query params and fragments for comparison)
            normalized_url = full_url.split('?')[0].split('#')[0]
            
            # Skip if we already have this URL
            if normalized_url in seen_urls:
                continue
            seen_urls.add(normalized_url)
            
            # Get link text for title
            link_text = link.get_text().strip()
            if not link_text:
                # Try to find title in parent elements
                parent = link.parent
                if parent:
                    link_text = parent.get_text().strip()[:100]  # Limit length
            if not link_text:
                link_text = 'Unknown'
            
            # Try to get embed HTML, but add track even if it fails
            embed_html = get_bandcamp_embed(full_url)
            
            tracks.append({
                'url': full_url,
                'embed_html': embed_html,  # Can be None
                'title': link_text
            })
            
            if len(tracks) >= 10:  # Get more tracks, filter by embed later if needed
                break
        
        print(f"Bandcamp search: Found {len(tracks)} tracks")
        
        result = {
            'search_url': search_url,
            'tracks': tracks
        }
        
        return result
    except Exception as e:
        print(f"Bandcamp search error: {str(e)}")
        import traceback
        traceback.print_exc()
        return {'search_url': search_url, 'tracks': []}

def get_bandcamp_embed(bandcamp_url):
    """Generate Bandcamp iframe embed HTML from a Bandcamp URL by scraping the page for numeric IDs."""
    try:
        # Normalize the URL
        if not bandcamp_url.startswith('http'):
            if bandcamp_url.startswith('/'):
                bandcamp_url = f"https://bandcamp.com{bandcamp_url}"
            else:
                bandcamp_url = f"https://{bandcamp_url}"
        
        # Fetch the Bandcamp page to extract numeric IDs
        response = make_request(bandcamp_url)
        if not response:
            return None
        
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Method 1: Look for existing embed code in the page
        embed_iframe = soup.find('iframe', {'src': lambda x: x and 'bandcamp.com/EmbeddedPlayer' in x})
        if embed_iframe:
            # Found an iframe, extract and modify it
            embed_src = embed_iframe.get('src')
            # Adjust height for our display (120px instead of 470px)
            embed_html = f'<iframe style="border: 0; width: 100%; height: 120px;" src="{embed_src}" seamless></iframe>'
            return embed_html
        
        # Method 2: Extract numeric IDs from JavaScript or data attributes
        track_id = None
        album_id = None
        
        # Look for data attributes
        track_elem = soup.find(attrs={'data-track-id': True})
        if track_elem:
            track_id = track_elem.get('data-track-id')
        
        album_elem = soup.find(attrs={'data-album-id': True})
        if album_elem:
            album_id = album_elem.get('data-album-id')
        
        # Search in JavaScript for numeric IDs
        scripts = soup.find_all('script')
        for script in scripts:
            if script.string:
                script_text = script.string
                
                # Look for patterns like "album_id": 1044201631 or album_id:1044201631
                album_match = re.search(r'["\']?album_id["\']?\s*[:=]\s*(\d+)', script_text)
                if album_match and not album_id:
                    album_id = album_match.group(1)
                
                # Look for track_id
                track_match = re.search(r'["\']?track_id["\']?\s*[:=]\s*(\d+)', script_text)
                if track_match and not track_id:
                    track_id = track_match.group(1)
                
                # Try TralbumData pattern - more comprehensive
                tralbum_patterns = [
                    r'TralbumData\s*=\s*\{[^}]*["\']?id["\']?\s*[:=]\s*(\d+)',
                    r'var\s+TralbumData\s*=\s*\{[^}]*["\']?id["\']?\s*[:=]\s*(\d+)',
                    r'["\']?id["\']?\s*[:=]\s*(\d+)[^}]*TralbumData',
                ]
                for pattern in tralbum_patterns:
                    tralbum_match = re.search(pattern, script_text, re.DOTALL)
                    if tralbum_match:
                        found_id = tralbum_match.group(1)
                        if '/track/' in bandcamp_url and not track_id:
                            track_id = found_id
                        elif '/album/' in bandcamp_url and not album_id:
                            album_id = found_id
                        break
                
                # Look for embed URLs in the page that contain IDs
                embed_url_match = re.search(r'bandcamp\.com/EmbeddedPlayer/(?:album=(\d+)|track=(\d+))', script_text)
                if embed_url_match:
                    if embed_url_match.group(1) and not album_id:
                        album_id = embed_url_match.group(1)
                    if embed_url_match.group(2) and not track_id:
                        track_id = embed_url_match.group(2)
                
        # Look for data-tralbum-id or similar attributes in HTML (outside script loop)
        tralbum_elem = soup.find(attrs={'data-tralbum-id': True})
        if tralbum_elem:
            tralbum_id = tralbum_elem.get('data-tralbum-id')
            if '/track/' in bandcamp_url and not track_id:
                track_id = tralbum_id
            elif '/album/' in bandcamp_url and not album_id:
                album_id = tralbum_id
        
        # Also try to find IDs in the page URL or meta tags
        meta_album = soup.find('meta', {'property': 'og:url'})
        if meta_album:
            meta_url = meta_album.get('content', '')
            # Extract IDs from URL if present
            album_match = re.search(r'/album/(\d+)', meta_url)
            if album_match and not album_id:
                album_id = album_match.group(1)
            track_match = re.search(r'/track/(\d+)', meta_url)
            if track_match and not track_id:
                track_id = track_match.group(1)
        
        # Build embed URL with found IDs
        if album_id:
            if track_id:
                # Both album and track - use track format with album context
                embed_url = f"https://bandcamp.com/EmbeddedPlayer/album={album_id}/size=large/bgcol=ffffff/linkcol=0687f5/tracklist=false/track={track_id}/transparent=true/"
            else:
                # Just album
                embed_url = f"https://bandcamp.com/EmbeddedPlayer/album={album_id}/size=large/bgcol=ffffff/linkcol=0687f5/tracklist=false/transparent=true/"
        elif track_id:
            # Just track
            embed_url = f"https://bandcamp.com/EmbeddedPlayer/track={track_id}/size=large/bgcol=ffffff/linkcol=0687f5/tracklist=false/artwork=small/transparent=true/"
        else:
            # Couldn't find IDs, return None
            print(f"Could not extract Bandcamp IDs from: {bandcamp_url}")
            return None
        
        # Generate iframe HTML
        embed_html = f'<iframe style="border: 0; width: 100%; height: 120px;" src="{embed_url}" seamless></iframe>'
        
        return embed_html
    except Exception as e:
        print(f"Bandcamp embed error: {str(e)}")
        return None

def search_amazon(artist, title, store_domain='amazon.com'):
    """Search Amazon and return product previews."""
    search_query = f"{artist} {title}"
    search_url = f"https://www.{store_domain}/s?k={requests.utils.quote(search_query)}&i=digital-music"
    
    try:
        # Use more realistic headers for Amazon
        amazon_headers = DEFAULT_HEADERS.copy()
        amazon_headers['Referer'] = f'https://www.{store_domain}/'
        
        response = make_request(search_url, headers=amazon_headers, timeout=15)
        if not response:
            return {'search_url': search_url, 'products': []}
        
        soup = BeautifulSoup(response.text, 'html.parser')
        products = []
        
        # Find product containers (Amazon's structure may vary)
        product_containers = soup.find_all('div', {'data-component-type': 's-search-result'})
        
        for container in product_containers[:5]:  # Limit to 5 products
            try:
                # Extract product title
                title_elem = container.find('h2', class_='a-size-mini')
                if not title_elem:
                    title_elem = container.find('h2')
                title_text = title_elem.get_text().strip() if title_elem else 'Unknown'
                
                # Extract product URL
                link_elem = container.find('a', class_='a-link-normal')
                if not link_elem:
                    link_elem = container.find('h2').find('a') if container.find('h2') else None
                
                if link_elem and link_elem.get('href'):
                    product_url = link_elem['href']
                    if not product_url.startswith('http'):
                        product_url = f"https://www.{store_domain}{product_url}"
                else:
                    continue
                
                # Extract price
                price_elem = container.find('span', class_='a-price-whole')
                price = price_elem.get_text().strip() if price_elem else None
                if price:
                    currency = container.find('span', class_='a-price-symbol')
                    currency_symbol = currency.get_text().strip() if currency else '$'
                    price = f"{currency_symbol}{price}"
                
                # Extract image
                img_elem = container.find('img', class_='s-image')
                image_url = img_elem.get('src') if img_elem else None
                
                # Try to get preview data
                preview_data = get_amazon_preview(product_url, store_domain)
                
                # Generate iframe embed HTML for Amazon product
                # Note: Amazon may block iframes, but we'll try anyway
                # NOTE: never combine allow-scripts with allow-same-origin --
                # together they let the framed page remove its own sandbox.
                embed_html = f'<iframe style="border: 0; width: 100%; height: 400px;" src="{product_url}" seamless sandbox="allow-scripts allow-popups allow-forms"></iframe>'
                
                products.append({
                    'title': title_text,
                    'url': product_url,
                    'price': price,
                    'image': image_url,
                    'preview': preview_data,
                    'embed_html': embed_html
                })
            except Exception as e:
                print(f"Error parsing Amazon product: {str(e)}")
                continue
        
        result = {
            'search_url': search_url,
            'products': products,
            'store_domain': store_domain
        }
        
        return result
    except Exception as e:
        print(f"Amazon search error: {str(e)}")
        return {'search_url': search_url, 'products': [], 'store_domain': store_domain}

def get_amazon_preview(product_url, store_domain='amazon.com'):
    """Scrape Amazon product page for preview data."""
    try:
        amazon_headers = DEFAULT_HEADERS.copy()
        amazon_headers['Referer'] = f'https://www.{store_domain}/'
        
        response = make_request(product_url, headers=amazon_headers, timeout=15)
        if not response:
            return None
        
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Look for audio preview
        preview_audio = None
        audio_elem = soup.find('audio', {'id': 'dmusic-preview-player'})
        if audio_elem:
            source_elem = audio_elem.find('source')
            if source_elem and source_elem.get('src'):
                preview_audio = source_elem['src']
        
        # Extract additional product info
        product_info = {
            'preview_audio': preview_audio,
            'description': None,
            'artist': None
        }
        
        # Try to get description
        desc_elem = soup.find('div', {'id': 'productDescription'})
        if desc_elem:
            product_info['description'] = desc_elem.get_text().strip()[:200]  # Limit length
        
        # Try to get artist/contributor info
        contributor_elem = soup.find('a', {'class': 'contributorNameID'})
        if contributor_elem:
            product_info['artist'] = contributor_elem.get_text().strip()
        
        return product_info
    except Exception as e:
        print(f"Amazon preview error: {str(e)}")
        return None

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

@app.route('/get-amazon-store', methods=['GET'])
def get_amazon_store():
    """Get detected Amazon store for the user."""
    detected_store = detect_amazon_store(request)
    return jsonify({'store_domain': detected_store})

@app.route('/search', methods=['POST'])
def search():
    url = request.json.get('url')
    amazon_store = request.json.get('amazon_store', 'amazon.com')

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
        
        # Fetch Bandcamp and Amazon data concurrently
        data = data_future.result()
        
        if 'error' in data:
            return jsonify(data)
        
        bandcamp_future = executor.submit(search_bandcamp, data['artist'], data['title'])
        amazon_future = executor.submit(search_amazon, data['artist'], data['title'], amazon_store)
        
        bandcamp_result = bandcamp_future.result()
        amazon_result = amazon_future.result()
    
    return jsonify({
        'source': source,
        'download_url': data.get('download_url'),
        'bandcamp': bandcamp_result,
        'amazon': amazon_result,
        'title': data['title'],
        'artist': data['artist']
    })

# Add a new route for keyword search
@app.route('/keyword-search', methods=['POST'])
def keyword_search():
    keywords = request.json.get('keywords')
    amazon_store = request.json.get('amazon_store', 'amazon.com')
    
    if not keywords:
        return jsonify({'error': 'No keywords provided'})
    
    # Use concurrent execution for better performance
    with ThreadPoolExecutor(max_workers=3) as executor:
        soundcloud_future = executor.submit(search_soundcloud, keywords)
        bandcamp_future = executor.submit(search_bandcamp, keywords, '')
        amazon_future = executor.submit(search_amazon, keywords, '', amazon_store)
        
        soundcloud_results = soundcloud_future.result()
        bandcamp_result = bandcamp_future.result()
        amazon_result = amazon_future.result()
    
    return jsonify({
        'soundcloud': soundcloud_results if soundcloud_results else None,
        'bandcamp': bandcamp_result,
        'amazon': amazon_result
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

@app.route('/get-bandcamp-embed', methods=['POST'])
def get_bandcamp_embed_route():
    """Get Bandcamp embed HTML for a URL."""
    bandcamp_url = request.json.get('url')
    if not bandcamp_url:
        return jsonify({'error': 'No URL provided'})

    if not is_allowed_url(bandcamp_url, ALLOWED_BANDCAMP_DOMAINS):
        return jsonify({'error': 'Disallowed URL'})

    embed_html = get_bandcamp_embed(bandcamp_url)
    if embed_html:
        return jsonify({'embed_html': embed_html})
    else:
        return jsonify({'error': 'Could not generate embed'})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)