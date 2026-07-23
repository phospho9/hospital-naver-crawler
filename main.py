import os
import re
import time
import random
import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# 1. 환경 변수 로드 (GitHub Secrets)
# ---------------------------------------------------------------------------
CF_ACCOUNT_ID = os.environ.get("CF_ACCOUNT_ID")
CF_DATABASE_ID = os.environ.get("CF_DATABASE_ID")
CF_API_TOKEN = os.environ.get("CF_API_TOKEN")

D1_API_URL = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/d1/database/{CF_DATABASE_ID}/query"
HEADERS = {
    "Authorization": f"Bearer {CF_API_TOKEN}",
    "Content-Type": "application/json"
}

# ---------------------------------------------------------------------------
# 2. Cloudflare D1 SQL 실행 함수
# ---------------------------------------------------------------------------
def execute_d1_query(sql, params=[]):
    payload = {"sql": sql, "params": params}
    try:
        res = requests.post(D1_API_URL, headers=HEADERS, json=payload, timeout=15)
        if res.status_code == 200:
            data = res.json()
            if data.get("success"):
                return data["result"][0].get("results", [])
            else:
                print(f"❌ D1 Query Failed: {data.get('errors')}")
        else:
            print(f"❌ D1 HTTP Error [{res.status_code}]: {res.text}")
    except Exception as e:
        print(f"❌ D1 Connection Exception: {e}")
    return None

# ---------------------------------------------------------------------------
# 3. 검색 키워드 정제 함수
# ---------------------------------------------------------------------------
def build_search_query(name, address):
    # 법인명 및 특수문자 제거
    clean_name = re.sub(r'\(주\)|\(유\)|의료법인|재단법인|법인', '', name).strip()
    
    # 주소에서 구/군 및 도로명 추출하여 검색 정확도 향상
    addr_parts = address.split() if address else []
    short_addr = ""
    if len(addr_parts) >= 3:
        short_addr = f"{addr_parts[1]} {addr_parts[2]}"
    elif len(addr_parts) >= 2:
        short_addr = addr_parts[1]
        
    return f"{short_addr} {clean_name}".strip()

# ---------------------------------------------------------------------------
# 4. 네이버 모바일 통합검색 HTML 크롤링
# ---------------------------------------------------------------------------
def crawl_naver_place(query):
    url = f"https://m.search.naver.com/search.naver?query={query}"
    
    # 아이폰 모바일 Safari 브라우저 헤더 위장
    headers = {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": "https://m.naver.com/"
    }
    
    try:
        res = requests.get(url, headers=headers, timeout=10)
        if res.status_code != 200:
            print(f"  ⚠️ HTTP Status Code: {res.status_code}")
            return None
            
        soup = BeautifulSoup(res.text, "html.parser")
        
        # 네이버 검색 결과 내 플레이스 영역 및 관련 카드 텍스트 수집
        text_elements = soup.select(".api_subject_bx, .place_section, .sc_new")
        
        collected_texts = []
        if text_elements:
            for elem in text_elements:
                collected_texts.append(elem.get_text(separator=" ", strip=True))
        else:
            # 특수 레이아웃 대비 전체 텍스트 백업 수집
            collected_texts.append(soup.get_text(separator=" ", strip=True))
            
        full_text = " ".join(collected_texts)
        
        # 의미 있는 길이의 검색 결과가 존재하는지 확인 (20자 이상)
        if len(full_text) > 20:
            return full_text
        return None
        
    except Exception as e:
        print(f"  ❌ Crawling Exception for {query}: {e}")
        return None

# ---------------------------------------------------------------------------
# 5. 텍스트 분석 및 키워드/특성 플래그 추출
# ---------------------------------------------------------------------------
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

# ---------------------------------------------------------------------------
# 6. 메인 파이프라인 실행
# ---------------------------------------------------------------------------
def main():
    LIMIT = 50
    print(f"🔍 Fetching {LIMIT} target hospitals from Cloudflare D1...")
    
    # description이 아직 수집되지 않은(NULL) 병원만 추출
    sql_select = f"SELECT id, name, address FROM hospitals WHERE description IS NULL ORDER BY id ASC LIMIT {LIMIT}"
    hospitals = execute_d1_query(sql_select)
    
    if hospitals is None:
        print("❌ D1 API Connection Failed. Check Secrets settings.")
        return

    if len(hospitals) == 0:
        print("🎉 No target hospitals found. Database is completely up to date!")
        return

    print(f"🚀 Found {len(hospitals)} targets. Starting crawling pipeline...\n")

    for idx, h in enumerate(hospitals, 1):
        h_id = h["id"]
        h_name = h["name"]
        h_addr = h.get("address", "")
        
        query = build_search_query(h_name, h_addr)
        print(f"[{idx}/{len(hospitals)}] Processing ID [{h_id[:12]}...] : {query}")
        
        crawled_text = crawl_naver_place(query)
        
        if crawled_text:
            flags = parse_flags(crawled_text)
            
            # 수집된 텍스트는 너무 길 수 있으므로 핵심 요약분(상위 500자)만 description에 저장
            summary_text = crawled_text[:500]
            
            sql_update = """
                UPDATE hospitals 
                SET description = ?, is_silbi = ?, has_chuna = ?, has_night = ?, 
                    has_365 = ?, has_yakchim = ?, is_cheopyak = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """
            params = [
                summary_text, flags['is_silbi'], flags['has_chuna'], flags['has_night'],
                flags['has_365'], flags['has_yakchim'], flags['is_cheopyak'], h_id
            ]
            execute_d1_query(sql_update, params)
            print(f"  ✅ Updated: {h_name} (Flags -> Chuna:{flags['has_chuna']}, Night:{flags['has_night']}, Silbi:{flags['is_silbi']})")
        else:
            # 검색 결과가 없는 경우 'N/A'로 기록하여 다음 루프에서 재조회되지 않도록 방지
            sql_update_empty = "UPDATE hospitals SET description = 'N/A', updated_at = CURRENT_TIMESTAMP WHERE id = ?"
            execute_d1_query(sql_update_empty, [h_id])
            print(f"  ⚠️ No result found for {h_name}. Marked as N/A.")

        # 네이버 차단 방지: 2초~3.5초 사이의 랜덤 대기시간 부여
        time.sleep(random.uniform(2.0, 3.5))

    print("\n✨ Daily Crawling Pipeline Completed Successfully!")

if __name__ == "__main__":
    main()
