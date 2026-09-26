import os
from flask import Flask, request, jsonify, render_template
from bs4 import BeautifulSoup
import requests
import re
import html
import unicodedata
from urllib.parse import urlparse
from textblob import TextBlob

def sanitize_text(text):
    if not text:
        return ''
    text = html.unescape(text)
    text = re.sub(r'[\x00-\x1F\x7F-\x9F]+', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def is_gibberish_text(text):
    if not text:
        return True
    text = text.strip()
    if len(text) < 20:
        return True
    total = len(text)
    good_chars = sum(
        1 for ch in text
        if ch.isalnum() or ch.isspace() or unicodedata.category(ch).startswith(('L', 'N', 'P', 'Z'))
    )
    if good_chars / total < 0.65:
        return True
    if re.search(r'(.)\1{6,}', text):
        return True
    weird_control = sum(1 for ch in text if unicodedata.category(ch).startswith('C'))
    if weird_control / total > 0.05:
        return True
    return False


def normalize_review_text(text):
    if not text:
        return ''
    text = sanitize_text(text)
    text = re.sub(r'(?i)brief content visible,?\s*double tap to read full content\.?', '', text)
    text = re.sub(r'(?i)full content visible,?\s*double tap to read brief content\.?', '', text)
    text = re.sub(r'(?i)double tap to read full content\.?', '', text)
    text = re.sub(r'(?i)read more\b', '', text)
    text = re.sub(r'(?i)read less\b', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text

def is_placeholder_review(text):
    if not text:
        return True
    lower = text.lower()
    return any(
        token in lower for token in [
            'brief content visible',
            'double tap to read full content',
            'full content visible',
            'read more',
            'read less',
            'read full content',
            'read brief content'
        ]
    )

# Optional browser-based fallback for JS-rendered/expanded reviews
def browser_fallback_extract(url, selectors, max_reviews=30):
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        return [], False, [{'url': url, 'status': 'no_playwright', 'blocked': False, 'reason': 'playwright not installed'}]

    attempts = []
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_extra_http_headers({
                'Accept-Language': 'en-US,en;q=0.9'
            })
            page.goto(url, timeout=30000)
            # give page a moment to load dynamic content
            page.wait_for_timeout(1500)

            # try to click 'Read more' expander buttons commonly used in Amazon reviews
            try:
                page.eval_on_selector_all('span[data-action="cr-expand-review"]', 'els => els.forEach(e => e.click())')
            except Exception:
                pass
            try:
                page.eval_on_selector_all('a[data-hook="review-title"]', 'els => els.forEach(e => e.click())')
            except Exception:
                pass

            texts = []
            seen = set()
            for sel in selectors:
                try:
                    elems = page.query_selector_all(sel)
                except Exception:
                    elems = []
                for el in elems:
                    try:
                        txt = (el.inner_text() or '').strip()
                    except Exception:
                        txt = ''
                    txt = normalize_review_text(txt)
                    if not txt or is_placeholder_review(txt) or is_gibberish_text(txt):
                        continue
                    if txt not in seen:
                        seen.add(txt)
                        texts.append(txt)
                        if len(texts) >= max_reviews:
                            break
                if len(texts) >= max_reviews:
                    break

            attempts.append({'url': url, 'status': 200, 'blocked': False, 'reason': 'playwright_success'})
            browser.close()
            return texts, False, attempts
    except Exception as exc:
        return [], False, [{'url': url, 'status': 'exception', 'blocked': False, 'reason': str(exc)}]

app = Flask(__name__, static_folder='static', template_folder='templates')

def detect_sarcasm(text):
    """
    Detect sarcasm based on explicit sarcastic phrases.
    """
    sarcastic_phrases = [
        "yeah right", "as if", "sure,", "of course", "whatever", 
        "oh great", "oh wonderful", "oh fantastic", "oh brilliant", 
        "real funny", "nice one", "good job", "sarcastic", "not really"
    ]
    lower = text.lower()
    return any(phrase in lower for phrase in sarcastic_phrases)

def detect_excitement(text):
    """
    Detect excitement based on exclamation marks and positive words.
    """
    excitement_words = [
        "wow", "amazing", "awesome", "excited", "love", 
        "great", "happy", "yay", "fantastic", "thrilled"
    ]
    # Check for excitement words
    if any(word in text.lower() for word in excitement_words):
        return True
    # Check for multiple exclamation marks
    if text.count("!") >= 2:
        return True
    return False

def get_asin_from_url(url):
    patterns = [
        r'/dp/([A-Z0-9]{10})',
        r'/gp/product/([A-Z0-9]{10})',
        r'/product-reviews/([A-Z0-9]{10})',
        r'/([A-Z0-9]{10})(?:[/?]|$)'
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None

def extract_amazon_reviews(product_url):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": "https://www.amazon.com/",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Cache-Control": "max-age=0"
    }

    def extract_reviews_from_soup(soup):
        reviews = []
        seen = set()

        # Prefer review blocks to avoid scraping unrelated page content.
        for block in soup.select('div[data-hook="review"]'):
            body = block.select_one(
                'span[data-hook="review-body"], div[data-hook="review-body"], '
                'span.a-size-base.review-text.review-text-content, div.review-text-content span, div.review-text'
            )
            if not body:
                continue
            text = normalize_review_text(body.get_text(" ", strip=True))
            if not text or is_placeholder_review(text) or is_gibberish_text(text):
                continue
            if text not in seen:
                seen.add(text)
                reviews.append(text)

        if reviews:
            return reviews

        # Fallback to broader selectors only if review blocks are unavailable.
        review_selectors = [
            'div[data-hook="review"] span[data-hook="review-body"]',
            'span[data-hook="review-body"]',
            'div[data-hook="review-body"]',
            'div[data-hook="review-collapsed"]',
            'span.a-size-base.review-text.review-text-content',
            'span.review-text-content',
            'span.a-size-base.review-text',
            'div.review-text-content span',
            'div.review-text',
            'div[data-hook="reviewText"]',
            'div[data-hook="reviewTextContainer"]',
            'div[class*="single-review-text-container"]',
            'div[class*="_Y3Itd_single-review-text-container"]',
            'div[data-hook*="review"] div[data-hook*="reviewText"]'
        ]
        for selector in review_selectors:
            found_reviews = soup.select(selector)
            for review in found_reviews:
                text = normalize_review_text(review.get_text(" ", strip=True))
                if not text or is_placeholder_review(text) or is_gibberish_text(text):
                    continue
                if text not in seen:
                    seen.add(text)
                    reviews.append(text)
        return reviews

    def fetch_page(session, url):
        try:
            response = session.get(url, timeout=20)
        except Exception as exc:
            print(f"Amazon fetch exception for {url}: {exc}")
            return None, False, {
                'url': url,
                'status': 'exception',
                'blocked': False,
                'reason': str(exc)
            }

        if response.status_code in (403, 503):
            print(f"Amazon page fetch blocked with status {response.status_code} for {url}")
            return None, True, {
                'url': url,
                'status': response.status_code,
                'blocked': True,
                'reason': 'HTTP block or service unavailable'
            }
        if response.status_code != 200:
            print(f"Amazon page fetch failed with status {response.status_code} for {url}")
            return None, False, {
                'url': url,
                'status': response.status_code,
                'blocked': False,
                'reason': 'HTTP error'
            }

        content_type = response.headers.get('Content-Type', '').lower()
        if 'text/html' not in content_type and 'application/xhtml+xml' not in content_type:
            print(f"Amazon response content type not HTML for {url}: {content_type}")
            return None, False, {
                'url': url,
                'status': response.status_code,
                'blocked': False,
                'reason': f'Unexpected content type: {content_type}'
            }

        response.encoding = response.apparent_encoding or response.encoding or 'utf-8'
        page_text = response.text.lower()
        blocked_tokens = [
            'captcha',
            'robot check',
            'sorry we just need to make sure',
            'enter the characters you see below',
            'verify you are a human',
            'type the characters you see in the image',
            'complete the security check to access'
        ]

        if any(token in page_text for token in blocked_tokens):
            print(f"Amazon bot check or blocked response detected for {url}")
            return None, True, {
                'url': url,
                'status': response.status_code,
                'blocked': True,
                'reason': 'Bot check content detected'
            }

        response_url = response.url.lower()
        if '/gp/sign-in' in response_url or '/ap/signin' in response_url:
            print(f"Amazon signin redirect detected for {url} -> {response.url}")
            return None, True, {
                'url': url,
                'status': response.status_code,
                'blocked': True,
                'reason': f'sign-in redirect to {response.url}'
            }

        soup = BeautifulSoup(response.text, 'html.parser')
        title_text = (soup.title.string or '').lower() if soup.title else ''
        if 'robot check' in title_text or 'security check' in title_text or 'access denied' in title_text:
            print(f"Amazon blocked page title detected for {url}: {title_text}")
            return None, True, {
                'url': url,
                'status': response.status_code,
                'blocked': True,
                'reason': f'Blocked page title: {title_text}'
            }

        return soup, False, {
            'url': url,
            'status': response.status_code,
            'blocked': False,
            'reason': 'success'
        }

    def build_review_urls(parsed, asin):
        scheme = parsed.scheme or 'https'
        hosts = [parsed.netloc, 'www.amazon.com', 'smile.amazon.com']
        urls = []
        url_paths = [
            f"/product-reviews/{asin}/?reviewerType=all_reviews&sortBy=recent&pageNumber=1",
            f"/product-reviews/{asin}/ref=cm_cr_arp_d_viewopt_srt?reviewerType=all_reviews&sortBy=recent&pageNumber=1",
            f"/gp/customer-reviews/{asin}/?ie=UTF8&reviewerType=all_reviews&pageNumber=1",
            f"/gp/aw/reviews/{asin}/?reviewerType=all_reviews&pageNumber=1",
            f"/dp/{asin}/#customerReviews"
        ]
        for host in hosts:
            for path in url_paths:
                urls.append(f"{scheme}://{host}{path}")
        return urls

    try:
        session = requests.Session()
        session.headers.update(headers)
        session.trust_env = False

        parsed = urlparse(product_url)
        asin = get_asin_from_url(product_url)

        if asin is None:
            print(f"No ASIN found in URL: {product_url}")
            return [], False, [{'url': product_url, 'status': 'invalid', 'blocked': False, 'reason': 'ASIN not found'}]

        urls_to_try = [product_url] + build_review_urls(parsed, asin)
        reviews = []
        blocked = False
        attempts = []
        for url in urls_to_try:
            soup, was_blocked, attempt_info = fetch_page(session, url)
            attempts.append(attempt_info)
            if was_blocked:
                blocked = True
                continue
            if soup is None:
                continue
            reviews = extract_reviews_from_soup(soup)
            if reviews:
                # if reviews are placeholders like 'Brief content visible', try browser fallback
                truncated_marker = 'brief content visible'
                low_reviews = [r.lower() for r in reviews]
                if any(truncated_marker in r for r in low_reviews):
                    # attempt browser fallback to expand full reviews
                    selectors = [
                        'div[data-hook="reviewText"]',
                        'div[data-hook="reviewTextContainer"]',
                        'div[data-hook="review"] div[data-hook="reviewText"]',
                        'span[data-hook="review-body"]',
                        'div.a-expander-content'
                    ]
                    b_reviews, b_blocked, b_attempts = browser_fallback_extract(url, selectors)
                    attempts.extend(b_attempts)
                    if b_reviews:
                        reviews = b_reviews
                        break
                else:
                    break

        return reviews, blocked, attempts

    except Exception as e:
        print(f"Error scraping reviews: {e}")
        return [], False, [{'url': product_url, 'status': 'exception', 'blocked': False, 'reason': str(e)}]

def extract_flipkart_reviews(product_url):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": "https://www.flipkart.com/",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Cache-Control": "max-age=0"
    }

    def extract_reviews_from_soup(soup):
        # Get all text and split into potential reviews
        reviews = []
        seen = set()
        
        # Find all text nodes that look like reviews
        for elem in soup.find_all(['div', 'p', 'span', 'section', 'article']):
            text = normalize_review_text(elem.get_text(" ", strip=True))
            
            # Filter based on length and content
            if not text or len(text) < 15 or is_gibberish_text(text):
                continue
            
            # Skip placeholders and UI text
            if is_placeholder_review(text):
                continue
            
            # Skip navigation/UI elements
            skip_words = ['helpful', 'not helpful', 'report', 'share', 'add to', 'buy', 'view all', 
                         'sort by', 'filter', 'flipkart', 'login', 'sign in', 'account', 'orders',
                         'wishlist', 'cart', 'search', 'categories', 'deals']
            if any(word in text.lower() for word in skip_words):
                continue
            
            # Skip very short common phrases
            if len(text) < 30 and text.lower() in ['click here', 'see more', 'loading', 'read more']:
                continue
            
            if text not in seen:
                reviews.append(text)
                seen.add(text)
        
        return reviews

    def fetch_page(session, url):
        try:
            response = session.get(url, timeout=20)
        except Exception as exc:
            print(f"Flipkart fetch exception for {url}: {exc}")
            return None, False, {
                'url': url,
                'status': 'exception',
                'blocked': False,
                'reason': str(exc)
            }

        if response.status_code in (403, 503):
            print(f"Flipkart page fetch blocked with status {response.status_code} for {url}")
            return None, True, {
                'url': url,
                'status': response.status_code,
                'blocked': True,
                'reason': 'HTTP block or service unavailable'
            }
        if response.status_code != 200:
            print(f"Flipkart page fetch failed with status {response.status_code} for {url}")
            return None, False, {
                'url': url,
                'status': response.status_code,
                'blocked': False,
                'reason': 'HTTP error'
            }

        content_type = response.headers.get('Content-Type', '').lower()
        if 'text/html' not in content_type and 'application/xhtml+xml' not in content_type:
            print(f"Flipkart response content type not HTML for {url}: {content_type}")
            return None, False, {
                'url': url,
                'status': response.status_code,
                'blocked': False,
                'reason': f'Unexpected content type: {content_type}'
            }

        response.encoding = response.apparent_encoding or response.encoding or 'utf-8'
        page_text = response.text.lower()
        # Only check for actual security challenges, not generic content
        blocked_tokens = ['captcha', 'robot check', 'security challenge']
        if any(token in page_text for token in blocked_tokens):
            print(f"Flipkart bot check detected for {url}")
            return None, True, {
                'url': url,
                'status': response.status_code,
                'blocked': True,
                'reason': 'Bot check content detected'
            }

        soup = BeautifulSoup(response.text, 'html.parser')
        return soup, False, {
            'url': url,
            'status': response.status_code,
            'blocked': False,
            'reason': 'success'
        }

    def build_review_urls(parsed, product_id):
        """Build multiple Flipkart review URLs to try"""
        scheme = parsed.scheme or 'https'
        netloc = parsed.netloc or 'www.flipkart.com'
        urls = []
        
        url_paths = [
            f"/p/{product_id}",
            f"/p/{product_id}?page=1",
            f"/product/{product_id}",
            f"/product/{product_id}?page=1",
            f"/d/{product_id}",
        ]
        
        for path in url_paths:
            urls.append(f"{scheme}://{netloc}{path}")
        
        return urls

    def get_product_id_from_url(url):
        """Extract product ID from Flipkart URL"""
        # Try to find /p/ pattern
        match = re.search(r'/p/([a-zA-Z0-9]+)', url)
        if match:
            return match.group(1)
        
        # Try to find /product/ pattern
        match = re.search(r'/product/([a-zA-Z0-9]+)', url)
        if match:
            return match.group(1)
        
        # Try to find /d/ pattern
        match = re.search(r'/d/([a-zA-Z0-9]+)', url)
        if match:
            return match.group(1)
        
        return None

    try:
        session = requests.Session()
        session.headers.update(headers)
        session.trust_env = False

        parsed = urlparse(product_url)
        product_id = get_product_id_from_url(product_url)

        if product_id is None:
            print(f"No product ID found in Flipkart URL: {product_url}")
            return [], False, [{'url': product_url, 'status': 'invalid', 'blocked': False, 'reason': 'Product ID not found'}]

        # Build multiple URLs to try
        urls_to_try = [product_url] + build_review_urls(parsed, product_id)
        
        reviews = []
        blocked = False
        attempts = []
        
        for url in urls_to_try:
            print(f"Trying Flipkart URL: {url}")
            soup, was_blocked, attempt_info = fetch_page(session, url)
            attempts.append(attempt_info)
            
            if was_blocked:
                blocked = True
                print(f"URL was blocked, trying next...")
                continue
            
            if soup is None:
                print(f"Failed to fetch, trying next URL...")
                continue
            
            reviews = extract_reviews_from_soup(soup)
            print(f"Extracted {len(reviews)} reviews from this URL")
            
            if reviews:
                print(f"Found reviews! Breaking.")
                break

        return reviews, blocked, attempts

    except Exception as e:
        print(f"Error scraping Flipkart reviews: {e}")
        import traceback
        traceback.print_exc()
        return [], False, [{'url': product_url, 'status': 'exception', 'blocked': False, 'reason': str(e)}]

def analyze_sentiment(reviews):
    """
    Analyze the sentiment of a list of reviews.
    """
    negative_phrases = [
        'not good', 'not worth', 'not worth it', 'not recommended',
        "don't buy", 'do not buy', 'worst', 'defective', 'broken',
        'disappointed', 'poor quality', 'not as good', 'no good',
        'bad', 'poor', 'damage', 'damaged', 'avoid', 'not a must buy'
    ]
    positive_phrases = [
        'worth the price', 'value for money', 'highly recommend',
        'good product', 'great product', 'very good', 'excellent',
        'love it', 'happy buying', 'worth price'
    ]

    results = []
    for review in reviews:
        blob = TextBlob(review)
        sentiment = blob.sentiment
        lower = review.lower()

        # Determine sentiment label
        if detect_sarcasm(review):
            sentiment_label = 'Sarcasm 😏'
        elif detect_excitement(review):
            sentiment_label = 'Excitement 🎉'
        elif any(phrase in lower for phrase in negative_phrases):
            sentiment_label = 'Negative 😠'
        elif any(phrase in lower for phrase in positive_phrases):
            sentiment_label = 'Positive 😊'
        elif sentiment.polarity > 0:
            sentiment_label = 'Positive 😊'
        elif sentiment.polarity < 0:
            sentiment_label = 'Negative 😠'
        else:
            sentiment_label = 'Neutral 😐'

        polarity = sentiment.polarity
        subjectivity = sentiment.subjectivity

        # Adjust weak polarity with strong phrase hints
        if sentiment_label == 'Negative 😠' and polarity > 0:
            polarity = min(polarity, -0.05)
        if sentiment_label == 'Positive 😊' and polarity < 0:
            polarity = max(polarity, 0.05)

        results.append({
            "review": review,
            "polarity": polarity,
            "subjectivity": subjectivity,
            "sentiment": sentiment_label
        })
    return results
@app.route('/')
def home():
    return render_template('index.html')

@app.route('/analyze', methods=['POST'])
def analyze_feedback():
    data = request.json
    product_url = data.get('url', '').strip()
    if not product_url:
        return jsonify({'error': 'No product URL provided'}), 400

    try:
        # Add https:// if no protocol is provided
        if not product_url.startswith(('http://', 'https://')):
            product_url = 'https://' + product_url
        
        # Choose extractor by domain
        parsed = urlparse(product_url)
        host = parsed.netloc.lower()
        platform = None
        if 'amazon.' in host:
            reviews, blocked, attempts = extract_amazon_reviews(product_url)
            platform = 'Amazon'
        elif 'flipkart.' in host:
            reviews, blocked, attempts = extract_flipkart_reviews(product_url)
            platform = 'Flipkart'
        else:
            return jsonify({'error': 'Unsupported domain. Only Amazon and Flipkart are supported.'}), 400
        
        if not reviews:
            error_payload = {
                'error': f'No reviews could be extracted from the {platform} URL',
                'attempts': attempts
            }
            if blocked:
                error_payload['error'] = f'{platform} blocked review access or page requires sign-in'
                return jsonify(error_payload), 502
            return jsonify(error_payload), 404

        # Analyze the sentiment of the reviews
        results = analyze_sentiment(reviews)
        response = {
            'reviews': results,
            'average_polarity': sum(result['polarity'] for result in results) / len(results),
            'average_subjectivity': sum(result['subjectivity'] for result in results) / len(results)
        }
        if blocked:
            response['warning'] = f'Some {platform} requests were blocked; review extraction succeeded from an alternate {platform} page.'
            response['attempts'] = attempts
        return jsonify(response)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
