# simplified_jefferson_test.py
# A simplified version to isolate the core scraping logic

import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin
import re
from datetime import datetime

BASE = "https://jeffersoncountytx.gov/InmateSearch"
SEARCH_URL = f"{BASE}/Search/List"

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

class SimplifiedJeffersonTest:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(UA)
    
    def test_search(self, last_name, first_name=None, use_wildcard=False):
        """Test a single search and return results"""
        print(f"\n--- Testing search: {last_name} {first_name or ''} (wildcard: {use_wildcard}) ---")
        
        # Build parameters
        params = {"lastName": last_name + ("*" if use_wildcard else "")}
        if first_name:
            params["firstName"] = first_name + ("*" if use_wildcard else "")
        
        try:
            print(f"Request URL: {SEARCH_URL}")
            print(f"Parameters: {params}")
            
            response = self.session.get(SEARCH_URL, params=params, timeout=30)
            print(f"Response Status: {response.status_code}")
            print(f"Final URL: {response.url}")
            
            if response.status_code != 200:
                print(f"ERROR: Non-200 status code: {response.status_code}")
                return []
            
            # Check for common error messages
            if re.search(r"too many|narrow your search|exceeded", response.text, re.I):
                print("WARNING: 'Too many results' message detected")
                return []
            
            # Extract detail links using the same logic as your scraper
            links = self._extract_detail_links(response.text)
            print(f"Detail links found: {len(links)}")
            
            # Save the response for debugging
            filename = f"test_search_{last_name}_{first_name or 'none'}.html"
            with open(filename, 'w', encoding='utf-8') as f:
                f.write(response.text)
            print(f"Response saved to: {filename}")
            
            return links
            
        except Exception as e:
            print(f"ERROR in search: {e}")
            return []
    
    def _extract_detail_links(self, html):
        """Extract detail links - copied from your scraper logic"""
        soup = BeautifulSoup(html or "", "lxml")
        links = []

        def _abs(u):
            return urljoin(BASE + "/", u)

        def _is_detail(href):
            if not href:
                return False
            h = href.lower()
            return bool(re.search(r"/inmatesearch/(search/)?detail(s)?(?:/|\?|$)", h))

        # Check regular links
        for a in soup.select("a[href]"):
            href = a.get("href", "").strip()
            if _is_detail(href):
                links.append(_abs(href))

        # Check clickable rows
        for row in soup.select(".clickable-row[data-href]"):
            dh = (row.get("data-href") or "").strip()
            if _is_detail(dh):
                links.append(_abs(dh))

        # Check onclick handlers
        for el in soup.select("[onclick]"):
            oc = el.get("onclick", "")
            m = re.search(r"location(?:\.href)?\s*=\s*['\"]([^'\"]+)['\"]", oc, re.I)
            if m:
                cand = m.group(1).strip()
                if _is_detail(cand):
                    links.append(_abs(cand))

        # Remove duplicates while preserving order
        seen = set()
        unique_links = []
        for link in links:
            if link not in seen:
                seen.add(link)
                unique_links.append(link)
        
        return unique_links
    
    def test_detail_page(self, detail_url):
        """Test fetching and parsing a detail page"""
        print(f"\n--- Testing detail page: {detail_url} ---")
        
        try:
            response = self.session.get(detail_url, timeout=30)
            print(f"Detail page status: {response.status_code}")
            print(f"Final URL: {response.url}")
            
            if response.status_code != 200:
                print(f"ERROR: Non-200 status for detail page")
                return None
            
            # Check if we got redirected away from inmate search
            if "/Sheriff" in response.url and "/InmateSearch/" not in response.url:
                print("ERROR: Redirected away from inmate search")
                return None
            
            # Check if it looks like an inmate detail page
            if not self._looks_like_inmate_detail(response.text):
                print("ERROR: Doesn't look like an inmate detail page")
                return None
            
            # Try to parse basic info
            soup = BeautifulSoup(response.text, 'lxml')
            
            # Look for name
            name_candidates = soup.select("h1, h2, .inmate-name, #inmate-name, [aria-level='1']")
            name = name_candidates[0].get_text(strip=True) if name_candidates else "Not found"
            print(f"Inmate name: {name}")
            
            # Look for key-value pairs
            kv_data = self._extract_kv_pairs(soup)
            print(f"Key-value pairs found: {len(kv_data)}")
            for key, value in list(kv_data.items())[:5]:  # Show first 5
                print(f"  {key}: {value}")
            
            # Look for charges table
            charges = self._extract_charges(soup)
            print(f"Charges found: {len(charges)}")
            
            # Save the detail page
            filename = f"test_detail_{re.sub(r'[^a-zA-Z0-9]', '_', detail_url[-50:])}.html"
            with open(filename, 'w', encoding='utf-8') as f:
                f.write(response.text)
            print(f"Detail page saved to: {filename}")
            
            return {
                "name": name,
                "kv_data": kv_data,
                "charges": charges,
                "url": response.url
            }
            
        except Exception as e:
            print(f"ERROR testing detail page: {e}")
            return None
    
    def _looks_like_inmate_detail(self, html):
        """Check if HTML looks like an inmate detail page"""
        soup = BeautifulSoup(html or "", "lxml")
        nameish = soup.select_one("h1, h2, .inmate-name, #inmate-name, [aria-level='1']")
        labels = soup.find(string=re.compile(r"(DOB|Date of Birth|Booking|Booked|Arrest)", re.I))
        charges = soup.find(string=re.compile(r"charge", re.I))
        return bool(nameish or labels or charges)
    
    def _extract_kv_pairs(self, soup):
        """Extract key-value pairs from the page"""
        out = {}
        
        # Check definition lists
        for dl in soup.select("dl"):
            dts = dl.select("dt")
            dds = dl.select("dd")
            for dt, dd in zip(dts, dds):
                key = dt.get_text(strip=True) if dt else ""
                value = dd.get_text(strip=True) if dd else ""
                if key:
                    out[key] = value
        
        # Check tables with th/td pairs
        for table in soup.select("table"):
            for row in table.select("tr"):
                th = row.find("th")
                td = row.find("td")
                if th and td:
                    key = th.get_text(strip=True)
                    value = td.get_text(strip=True)
                    if key:
                        out[key] = value
        
        return out
    
    def _extract_charges(self, soup):
        """Extract charges from tables"""
        charges = []
        
        for table in soup.select("table"):
            headers = [th.get_text(strip=True).lower() for th in table.select("thead th")] or \
                     [th.get_text(strip=True).lower() for th in table.find_all("th")]
            
            if not headers or not any("charge" in h for h in headers):
                continue
            
            # Find column indices
            charge_idx = next((i for i, h in enumerate(headers) if "charge" in h), None)
            status_idx = next((i for i, h in enumerate(headers) if "status" in h), None)
            bond_idx = next((i for i, h in enumerate(headers) if "bond" in h), None)
            
            for row in table.select("tbody tr") or table.select("tr"):
                cells = row.find_all("td")
                if not cells:
                    continue
                
                charge_data = {}
                if charge_idx is not None and charge_idx < len(cells):
                    charge_data["charge"] = cells[charge_idx].get_text(strip=True)
                if status_idx is not None and status_idx < len(cells):
                    charge_data["status"] = cells[status_idx].get_text(strip=True)
                if bond_idx is not None and bond_idx < len(cells):
                    charge_data["bond"] = cells[bond_idx].get_text(strip=True)
                
                if charge_data.get("charge"):
                    charges.append(charge_data)
        
        return charges

def main():
    print("Simplified Jefferson County Scraper Test")
    print("=" * 50)
    
    tester = SimplifiedJeffersonTest()
    
    # Test different search strategies
    test_cases = [
        ("SMITH", None, False),      # Simple last name
        ("JOHNSON", None, False),    # Another common name
        ("BROWN", "J", False),       # With first initial
        ("GARCIA", None, True),      # With wildcard
        ("WILSON", "M", True),       # First + last with wildcards
    ]
    
    successful_links = []
    
    for last_name, first_name, use_wildcard in test_cases:
        links = tester.test_search(last_name, first_name, use_wildcard)
        if links:
            successful_links.extend(links[:2])  # Take first 2 from each successful search
            print(f"SUCCESS: Found {len(links)} links for {last_name} {first_name or ''}")
        else:
            print(f"No results for {last_name} {first_name or ''}")
    
    # Test detail pages if we found any
    if successful_links:
        print(f"\nTesting {len(successful_links)} detail pages...")
        for i, link in enumerate(successful_links[:3]):  # Test first 3
            result = tester.test_detail_page(link)
            if result:
                print(f"SUCCESS: Parsed detail page {i+1}")
            else:
                print(f"FAILED: Could not parse detail page {i+1}")
    else:
        print("\nNo detail links found to test")
    
    print("\n" + "=" * 50)
    print("Test completed. Check the generated HTML files for debugging.")

if __name__ == "__main__":
    main()