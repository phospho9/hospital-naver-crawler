import os
import re
import time
import requests
from bs4 import BeautifulSoup

# 환경 변수 (GitHub Secrets에서 불러옴)
CF_ACCOUNT_ID = os.environ.get("CF_ACCOUNT_ID")
CF_DATABASE_ID = os.environ.get("CF_DATABASE_ID")
CF_API_TOKEN = os.environ.get("CF_API_TOKEN")

D1_API_URL = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/d1/database/{CF_DATABASE_ID}/query"
HEADERS = {
    "Authorization": f"Bearer {CF_API_TOKEN}",
    "Content-Type": "application/json"
}

# 1. Cloudflare D1 SQL 실행 함수
def execute_d1_query(sql, params=[]):
    payload = {"sql": sql, "params": params}
    res = requests.post(D1_API_URL, headers=HEADERS, json=payload)
    if res.status_code == 200:
        data = res.json()
        if data.get("success"):
            return data["result"][0].get("results", [])
    print(f"D1 Query Error: {res.text}")
    return None

# 2. 주소 기반 검색어 최적화 함수
def build_search_query(name, address):
    clean_name = re.sub(r'\(주\)|\(유\)|의료법인|법인', '', name).strip()
    addr_parts = address.split()
    short_addr = ""
    if len(addr_parts) >= 3:
        short_addr = f"{addr_parts[1]} {addr_parts[2]}"
    elif len(addr_parts) >= 2:
        short_addr = addr_parts[1]
    return f"{short_addr} {clean_name}".strip()

# 3. 네이버 플레이스 크롤링 및 텍스트 취합
def crawl_naver_place(query):
    search_url = f"https://m.map.naver.com/search2/searchMore.naver?query={query}&page=1"
    headers = {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"
    }
    
    try:
        res = requests.get(search_url, headers=headers, timeout=10)
        if res.status_code != 200:
            return None
            
        data = res.json()
        result_site = data.get("result", {}).get("site", {})
        items = result_site.get("list", [])
        
        if not items:
            return None
            
        # 첫 번째 검색 결과 아이템
        item = items[0]
        
        # 소개글, 태그, 시간정보 취합
        description = item.get("description", "") or ""
        tags = " ".join(item.get("microReview", [])) if item.get("microReview") else ""
        options = " ".join(item.get("options", [])) if item.get("options") else ""
        
        combined_text = f"{description} {tags} {options}".strip()
        return combined_text
    except Exception as e:
        print(f"Crawling error for {query}: {e}")
        return None

# 4. 텍스트 분석 및 플래그 추출
def parse_flags(text):
    if not text:
        return {
            'is_silbi': 0, 'has_chuna': 0, 'has_night': 0,
            'has_365': 0, 'has_yakchim': 0, 'is_cheopyak': 0
        }
    
    t = text.lower()
    return {
        'is_silbi': 1 if re.search(r'(실비|실손|도수|도수치료)', t) else 0,
        'has_chuna': 1 if re.search(r'(추나|추나요법|척추교정|체형교정)', t) else 0,
        'has_night': 1 if re.search(r'(야간|야간진료|밤진료|20:|21:)', t) else 0,
        'has_365': 1 if re.search(r'(365|일요일|공휴일|주말진료)', t) else 0,
        'has_yakchim': 1 if re.search(r'(약침|봉약침|봉침|벌침)', t) else 0,
        'is_cheopyak': 1 if re.search(r'(첩약|한약보험|건강보험한약)', t) else 0,
    }

# 메인 실행 함수
def main():
    # 하루 처리 수량 (네이버 차단 방지용 50개)
    LIMIT = 50
    print(f"Fetching {LIMIT} target hospitals from D1...")
    
    # description이 아직 없는 병원 50개 추출
    sql_select = "SELECT id, name, address FROM hospitals WHERE description IS NULL ORDER BY id ASC LIMIT ?"
    hospitals = execute_d1_query(sql_select, [LIMIT])
    
    if not hospitals:
        print("No target hospitals found. All up to date!")
        return

    print(f"Found {len(hospitals)} targets. Starting crawling...")

    for h in hospitals:
        h_id = h["id"]
        h_name = h["name"]
        h_addr = h["address"]
        
        query = build_search_query(h_name, h_addr)
        print(f"Processing ID [{h_id}] : {query}")
        
        crawled_text = crawl_naver_place(query)
        
        if crawled_text:
            flags = parse_flags(crawled_text)
            sql_update = """
                UPDATE hospitals 
                SET description = ?, is_silbi = ?, has_chuna = ?, has_night = ?, 
                    has_365 = ?, has_yakchim = ?, is_cheopyak = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """
            params = [
                crawled_text, flags['is_silbi'], flags['has_chuna'], flags['has_night'],
                flags['has_365'], flags['has_yakchim'], flags['is_cheopyak'], h_id
            ]
            execute_d1_query(sql_update, params)
            print(f" Successfully updated {h_name}")
        else:
            # 검색 결과가 없는 경우 빈 값으로 채워 다음번에 재검색되지 않도록 처리
            sql_update_empty = "UPDATE hospitals SET description = 'N/A', updated_at = CURRENT_TIMESTAMP WHERE id = ?"
            execute_d1_query(sql_update_empty, [h_id])
            print(f" No result found for {h_name}. Marked as N/A.")

        # 네이버 서버 과부하/차단 방지를 위한 2초 대기
        time.sleep(2)

if __name__ == "__main__":
    main()
