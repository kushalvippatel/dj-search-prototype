from flask import Flask, render_template, request, jsonify
import requests
from bs4 import BeautifulSoup
import re
import os

app = Flask(__name__)

def get_soundcloud_data(url):
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        
        response = requests.get(url, headers=headers)
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
        
        return {
            'title': title,
            'artist': artist,
            'download_url': download_url
        }
            
    except Exception as e:
        return {'error': str(e)}

def search_bandcamp(artist, title):
    search_query = f"{artist} {title}"
    return f"https://bandcamp.com/search?q={requests.utils.quote(search_query)}"

def search_amazon(artist, title):
    # Construct Amazon.com search URL specifically for digital music
    search_query = f"{artist} {title}"
    return f"https://www.amazon.com/s?k={requests.utils.quote(search_query)}&i=digital-music"

def get_youtube_data(url):
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        
        response = requests.get(url, headers=headers)
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
        
        return {
            'title': title,
            'artist': artist,
            'download_url': download_url
        }
            
    except Exception as e:
        print(f"YouTube scraping error: {str(e)}")  # Add debug printing
        return {'error': str(e)}

def get_spotify_data(url):
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'DNT': '1',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1'
        }
        
        response = requests.get(url, headers=headers)
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
            
        return {
            'title': title,
            'artist': artist,
            'download_url': None
        }
            
    except Exception as e:
        print(f"Spotify scraping error: {str(e)}")
        return {'error': str(e)}

def search_soundcloud(keywords):
    try:
        # Get the search URL
        search_query = keywords.replace(' ', '%20')
        search_url = f"https://soundcloud.com/search?q={search_query}"
        
        # Get the search results page
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        
        response = requests.get(search_url, headers=headers)
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
                    oembed_response = requests.get(oembed_url, headers=headers)
                    oembed_data = oembed_response.json()
                    tracks.append({
                        'url': track_url,
                        'embed_html': oembed_data['html']
                    })
                except:
                    continue
        
        return {
            'search_url': search_url,
            'tracks': tracks[:5]  # Return first 5 tracks with embed code
        }
            
    except Exception as e:
        print(f"SoundCloud search error: {str(e)}")
        return None

def get_track_info(track_url):
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        
        response = requests.get(track_url, headers=headers)
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Look for download links
        download_url = None
        for link in soup.find_all('a', href=True):
            href = link['href'].lower()
            link_text = link.get_text().lower()
            if any(word in href or word in link_text for word in ['download', 'free dl', 'buy', 'purchase']):
                download_url = link['href']
                break
                
        return {
            'download_url': download_url
        }
    except Exception as e:
        print(f"Error checking track: {str(e)}")
        return {'error': str(e)}

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/search', methods=['POST'])
def search():
    url = request.json.get('url')
    if not url:
        return jsonify({'error': 'No URL provided'})
    
    # Determine the source URL type
    if 'youtube.com' in url or 'youtu.be' in url:
        data = get_youtube_data(url)
        source = 'YouTube'
    elif 'spotify.com' in url:
        data = get_spotify_data(url)
        source = 'Spotify'
    else:
        data = get_soundcloud_data(url)
        source = 'SoundCloud'
    
    if 'error' in data:
        return jsonify(data)
    
    # Get Bandcamp and Amazon search URLs
    bandcamp_url = search_bandcamp(data['artist'], data['title'])
    amazon_url = search_amazon(data['artist'], data['title'])
    
    return jsonify({
        'source': source,
        'download_url': data.get('download_url'),
        'bandcamp_url': bandcamp_url,
        'amazon_url': amazon_url,
        'title': data['title'],
        'artist': data['artist']
    })

# Add a new route for keyword search
@app.route('/keyword-search', methods=['POST'])
def keyword_search():
    keywords = request.json.get('keywords')
    if not keywords:
        return jsonify({'error': 'No keywords provided'})
    
    soundcloud_results = search_soundcloud(keywords)
    bandcamp_url = search_bandcamp(keywords, '')
    amazon_url = search_amazon(keywords, '')
    
    return jsonify({
        'soundcloud_url': soundcloud_results['search_url'] if soundcloud_results else None,
        'tracks': soundcloud_results['tracks'] if soundcloud_results else None,
        'bandcamp_url': bandcamp_url,
        'amazon_url': amazon_url
    })

@app.route('/check-track', methods=['POST'])
def check_track():
    track_url = request.json.get('track_url')
    if not track_url:
        return jsonify({'error': 'No track URL provided'})
    
    track_info = get_track_info(track_url)
    return jsonify(track_info)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)