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
    clean_name = re.sub(r'\(주\)|\(유\)|의료법인|재단법인|법인', '', name).strip()
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
    headers = {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": "https://m.naver.com/"
    }
    
    try:
        res = requests.get(url, headers=headers, timeout=10)
        if res.status_code != 200:
            return None
            
        soup = BeautifulSoup(res.text, "html.parser")
        
        place_box = soup.select_one(".place_section, .api_subject_bx")
        if place_box:
            crawled_text = place_box.get_text(separator=" ", strip=True)
        else:
            crawled_text = soup.get_text(separator=" ", strip=True)
            
        return crawled_text[:1000]
        
    except Exception as e:
        print(f"  ❌ Crawling Exception for {query}: {e}")
        return None

# ---------------------------------------------------------------------------
# 5. 💡 [핵심] 텍스트 분석 및 플래그 매핑 (원장님 취합 로직 적용)
# ---------------------------------------------------------------------------
def parse_flags(text, name=""):
    flags = {
        'is_silbi': 0, 'has_chuna': 0, 'has_night': 0, 'has_365': 0, 
        'has_yakchim': 0, 'is_cheopyak': 0, 'has_parking': 0, 
        'has_ward': 0, 'is_traffic_acc': 0, 'business_hours': None, 'lunch_time': None
    }
    
    if not text:
        return flags
    
    t = text.lower() # 네이버 설명글 소문자 변환
    n = name.lower() # 병원 상호명 소문자 변환
    
    # 진료시간 / 점심시간 추출 (유지)
    hours_match = re.search(r'(\d{1,2}:\d{2})\s*[~–\-]\s*(\d{1,2}:\d{2})', t)
    if hours_match:
        flags['business_hours'] = f"{hours_match.group(1)} ~ {hours_match.group(2)}"
    
    lunch_match = re.search(r'(휴게|점심|브레이크)[^\d]*(\d{1,2}:\d{2})\s*[~–\-]\s*(\d{1,2}:\d{2})', t)
    if lunch_match:
        flags['lunch_time'] = f"{lunch_match.group(2)} ~ {lunch_match.group(3)}"

    # --- 원장님 맞춤형 추출 규칙 반영 ---

    # 1. 입원실 (상호명에 '병원', '요양병원'이 있거나 OR 설명글에 입원 키워드가 있는 경우)
    flags['has_ward'] = 1 if ('병원' in n or '요양병원' in n) or re.search(r'(입원실|입원병동|병실)', t) else 0

    # 2. 추나 특화 (순수하게 네이버 설명글 기준)
    flags['has_chuna'] = 1 if re.search(r'(추나|추나요법|척추교정)', t) else 0

    # 3. 약침 특화 (원장님이 짚어주신 '봉독' 키워드 포함)
    flags['has_yakchim'] = 1 if re.search(r'(약침|봉침|봉독|봉약침)', t) else 0

    # 4. 첩약건강보험 (설명글 기준, 다양한 단어 커버)
    flags['is_cheopyak'] = 1 if re.search(r'(첩약건강보험|첩약|한약|보약)', t) else 0

    # 5. 야간/휴일 진료 (설명글 기준 - '야간', '365', 시간 패턴 통합 추출)
    flags['has_night'] = 1 if re.search(r'(야간|야간진료|20:00|365|일요일|공휴일)', t) else 0
    flags['has_365'] = 1 if re.search(r'(365|일요일|공휴일|연중무휴)', t) else 0

    # 6. 기타 특화 진료 (설명글 기준 - 기존 기능 유지)
    flags['is_silbi'] = 1 if re.search(r'(실비|도수치료|체외충격파)', t) else 0
    flags['has_parking'] = 1 if re.search(r'(주차|무료주차|발렛|주차장)', t) else 0
    flags['is_traffic_acc'] = 1 if re.search(r'(교통사고|자동차보험|자보|교통사고후유증)', t) else 0

    # -----------------------------------------------------------
    # 🔥 생명마루 한의원 안산점 특별 가중치 (슈퍼 패스)
    # 네이버 플레이스 작성 상태와 무관하게 모든 홍보 배지가 켜집니다.
    # -----------------------------------------------------------
    if "생명마루" in name and "안산" in name:
        flags['has_chuna'] = 1
        flags['is_cheopyak'] = 1
        flags['has_yakchim'] = 1
        flags['has_night'] = 1
        flags['has_365'] = 1
        flags['has_parking'] = 1
        flags['has_ward'] = 0 # (입원실을 운영하지 않으신다면 0으로 두시면 됩니다)
        flags['is_traffic_acc'] = 1
        print("  ⭐ [슈퍼 패스] 생명마루 한의원 안산점 모든 특화진료 100% 매핑 완료")

    return flags

# ---------------------------------------------------------------------------
# 6. 메인 파이프라인 실행
# ---------------------------------------------------------------------------
def main():
    LIMIT = 50
    print(f"🔍 Fetching {LIMIT} target hospitals from Cloudflare D1...")
    
    # 업데이트가 안 된(description IS NULL) 한의원/한방병원 위주로 크롤링 진행
    sql_select = f"""
        SELECT id, name, address 
        FROM hospitals 
        WHERE (description IS NULL OR updated_at < datetime('now', '-90 days')) 
        AND (name LIKE '%한의원%' OR name LIKE '%한방병원%')
        ORDER BY updated_at ASC LIMIT {LIMIT}
    """
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
            flags = parse_flags(crawled_text, h_name)
            summary_text = crawled_text[:500]
            
            sql_update = """
                UPDATE hospitals 
                SET description = ?, is_silbi = ?, has_chuna = ?, has_night = ?, 
                    has_365 = ?, has_yakchim = ?, is_cheopyak = ?,
                    has_parking = ?, has_ward = ?, is_traffic_acc = ?,
                    business_hours = ?, lunch_time = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """
            params = [
                summary_text, flags['is_silbi'], flags['has_chuna'], flags['has_night'],
                flags['has_365'], flags['has_yakchim'], flags['is_cheopyak'],
                flags['has_parking'], flags['has_ward'], flags['is_traffic_acc'],
                flags['business_hours'], flags['lunch_time'], h_id
            ]
            execute_d1_query(sql_update, params)
            print(f"  ✅ Updated: {h_name} (Chuna:{flags['has_chuna']}, Yakchim:{flags['has_yakchim']}, Cheopyak:{flags['is_cheopyak']})")
        else:
            sql_update_empty = "UPDATE hospitals SET description = 'N/A', updated_at = CURRENT_TIMESTAMP WHERE id = ?"
            execute_d1_query(sql_update_empty, [h_id])
            print(f"  ⚠️ No result found for {h_name}. Marked as N/A.")

        time.sleep(random.uniform(2.0, 3.5))

    print("\n✨ Daily Naver Place Crawling Pipeline Completed Successfully!")

if __name__ == "__main__":
    main()
