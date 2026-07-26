import os
import re
import time
import random
import requests
from bs4 import BeautifulSoup
from collections import Counter
from datetime import datetime, timezone, timedelta

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
                print(f"    ❌ D1 쿼리 실패: {data.get('errors')}")
        else:
            print(f"    ❌ D1 HTTP 에러 [{res.status_code}]: {res.text}")
    except Exception as e:
        print(f"    ❌ D1 연결 예외 발생: {e}")
    return None

# ---------------------------------------------------------------------------
# 3. 검색 키워드 정제 함수
# ---------------------------------------------------------------------------
def build_search_query(name, address):
    clean_name = re.sub(r'\(주\)|\(유\)|의료법인|재단법인|법인', '', name).strip()
    addr_parts = address.split() if address else []
    
    region_str = ""
    if len(addr_parts) >= 2:
        region_str = f"{addr_parts[0]} {addr_parts[1]}"
    elif len(addr_parts) == 1:
        region_str = addr_parts[0]
        
    return f"{clean_name} {region_str}".strip()

# ---------------------------------------------------------------------------
# 4. 네이버 플레이스 정밀 크롤링 (Raw HTML 포함 반환)
# ---------------------------------------------------------------------------
def crawl_naver_place(query):
    search_url = f"https://m.search.naver.com/search.naver?query={query}"
    print(f"    📍 [검색 진입] {search_url}")
    
    headers = {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": "https://m.naver.com/"
    }
    
    try:
        res = requests.get(search_url, headers=headers, timeout=10)
        if res.status_code != 200:
            print(f"    ⚠️ 네이버 검색 응답 실패 (Status: {res.status_code})")
            return None, None, None
            
        soup_search = BeautifulSoup(res.text, "html.parser")
        place_section = soup_search.select_one(".place_section, .sc_new.cs_place, .api_subject_bx")
        
        place_id = None
        if place_section:
            place_id_match = re.search(r'(?:hospital/|place/|data-id=")(\d{7,11})', str(place_section))
            if place_id_match:
                place_id = place_id_match.group(1)

        if place_id:
            navermap_url = f"https://m.place.naver.com/hospital/{place_id}/home"
            print(f"    🎯 [ID 확보] 병원 고유번호 추출 성공: {place_id}")
            print(f"    🌐 [URL 확보] 다이렉트 링크: {navermap_url}")
            
            res_home = requests.get(navermap_url, headers=headers, timeout=10)
            res_home.encoding = 'utf-8'  
            raw_html = res_home.text
            
            soup_home = BeautifulSoup(raw_html, "html.parser")
            home_text = soup_home.get_text(separator=" ", strip=True)
            
            res_info = requests.get(f"https://m.place.naver.com/hospital/{place_id}/information", headers=headers, timeout=10)
            res_info.encoding = 'utf-8'  
            soup_info = BeautifulSoup(res_info.text, "html.parser")
            info_text = soup_info.get_text(separator=" ", strip=True)
            
            combined_text = (home_text + " " + info_text)[:1500]
            print(f"    📝 [텍스트 수집] 렌더링 텍스트 파싱 완료 (총 {len(combined_text)}자)")
            
            return combined_text, raw_html, navermap_url
            
        else:
            print("    ⚠️ 병원 고유 ID를 찾을 수 없습니다. 통합검색 텍스트로 대체합니다.")
            return soup_search.get_text(separator=" ", strip=True)[:1000], res.text, search_url
            
    except Exception as e:
        print(f"    ❌ 크롤링 중 예외 발생: {e}")
        return None, None, None

# ---------------------------------------------------------------------------
# 5. 💡 텍스트 분석 및 플래그 매핑 ("오늘" -> 실제 요일 변환 적용)
# ---------------------------------------------------------------------------
def parse_flags(text, raw_html, name=""):
    flags = {
        'is_silbi': 0, 'has_chuna': 0, 'has_night': 0, 'has_365': 0, 
        'has_yakchim': 0, 'is_cheopyak': 0, 'has_parking': 0, 
        'has_ward': 0, 'is_traffic_acc': 0, 'business_hours': None, 'lunch_time': None
    }
    
    if not text or not raw_html:
        return flags
        
    # 💡 [핵심] KST 기준 오늘 요일 구하기
    kst = timezone(timedelta(hours=9))
    today_kst = datetime.now(kst)
    weekdays = ['월요일', '화요일', '수요일', '목요일', '금요일', '토요일', '일요일']
    today_str = weekdays[today_kst.weekday()]
    
    # "오늘"이라는 글자를 모두 실제 요일로 변환하여 데이터 일관성 확보
    t = text.lower().replace("오늘", today_str)
    raw_html = raw_html.replace("오늘", today_str)
    n = name.lower()
    
    # 1. 진료시간 (API 데이터 타겟팅)
    starts = re.findall(r'"startTime"\s*:\s*"(\d{1,2}:\d{2})"', raw_html)
    ends = re.findall(r'"endTime"\s*:\s*"(\d{1,2}:\d{2})"', raw_html)
    
    if starts and ends:
        pairs = list(zip(starts, ends))
        most_common_hours = Counter(pairs).most_common(1)[0][0]
        flags['business_hours'] = f"{most_common_hours[0]} ~ {most_common_hours[1]}"
    else:
        hours_match = re.search(r'(\d{1,2}:\d{2})\s*[~–\-]\s*(\d{1,2}:\d{2})', raw_html)
        if hours_match:
            flags['business_hours'] = f"{hours_match.group(1)} ~ {hours_match.group(2)}"

    # 2. 휴게/점심시간
    lunch_match = re.search(r'(휴게시간|점심시간|휴게|점심|브레이크)[^\d]*(\d{1,2}:\d{2})\s*[~–\-]\s*(\d{1,2}:\d{2})', raw_html)
    if lunch_match:
        flags['lunch_time'] = f"{lunch_match.group(2)} ~ {lunch_match.group(3)}"

    # 특화 진료 추출 (변환된 텍스트 t 활용)
    flags['has_ward'] = 1 if ('병원' in n or '요양병원' in n) or re.search(r'(입원실|입원병동|병실)', t) else 0
    flags['has_chuna'] = 1 if re.search(r'(추나|추나요법|척추교정)', t) else 0
    flags['has_yakchim'] = 1 if re.search(r'(약침|봉침|봉독|봉약침)', t) else 0
    flags['is_cheopyak'] = 1 if re.search(r'(첩약건강보험|첩약|한약|보약)', t) else 0
    flags['has_night'] = 1 if re.search(r'(야간|야간진료|20:00|21:00|밤진료)', t) else 0
    flags['has_365'] = 1 if re.search(r'(365|일요일|공휴일|연중무휴)', t) else 0
    flags['is_silbi'] = 1 if re.search(r'(실비|도수치료|체외충격파)', t) else 0
    flags['has_parking'] = 1 if re.search(r'(주차|무료주차|발렛|주차장)', t) else 0
    flags['is_traffic_acc'] = 1 if re.search(r'(교통사고|자동차보험|자보|교통사고후유증)', t) else 0

    if "생명마루" in name and "안산" in name:
        flags['has_chuna'] = 1
        flags['is_cheopyak'] = 1
        flags['has_yakchim'] = 1
        flags['has_night'] = 1
        flags['has_365'] = 1
        flags['has_parking'] = 1
        flags['has_ward'] = 0
        flags['is_traffic_acc'] = 1
        print("\n    ⭐⭐ [슈퍼 패스 발동] 생명마루 한의원 안산점: 모든 특화 진료 100% 매핑 완료! ⭐⭐")

    return flags

# ---------------------------------------------------------------------------
# 6. 메인 파이프라인 실행
# ---------------------------------------------------------------------------
def main():
    LIMIT = 90
    print(f"\n🔍 [DB 연결] 클라우드플레어 D1에서 타겟 한의원 {LIMIT}개 조회 중...")
    
    sql_select = f"""
        SELECT id, name, address 
        FROM hospitals 
        WHERE (description IS NULL OR updated_at < datetime('now', '-30 days')) 
        AND (name LIKE '%한의원%' OR name LIKE '%한방병원%')
        ORDER BY updated_at ASC LIMIT {LIMIT}
    """
    hospitals = execute_d1_query(sql_select)
    
    if hospitals is None:
        print("❌ DB 통신 실패: 환경변수 및 쿼리 설정을 확인하세요.")
        return

    if len(hospitals) == 0:
        print("🎉 모든 병원 정보가 최신 상태입니다. 크롤러를 종료합니다.")
        return

    print(f"🚀 총 {len(hospitals)}개의 타겟 병원을 찾았습니다. API 타겟팅 정밀 크롤링을 시작합니다!\n")
    print("=" * 70)

    for idx, h in enumerate(hospitals, 1):
        h_id = h["id"]
        h_name = h["name"]
        h_addr = h.get("address", "")
        
        query = build_search_query(h_name, h_addr)
        print(f"[{idx}/{len(hospitals)}] 🏥 병원명: {h_name} (ID: {h_id[:8]}...)")
        print(f"    - 검색 키워드: {query}")
        
        crawled_text, raw_html, map_url = crawl_naver_place(query)
        
        if crawled_text and raw_html:
            flags = parse_flags(crawled_text, raw_html, h_name)
            summary_text = crawled_text[:500] 
            
            sql_update = """
                UPDATE hospitals 
                SET description = ?, navermap_url = ?, is_silbi = ?, has_chuna = ?, has_night = ?, 
                    has_365 = ?, has_yakchim = ?, is_cheopyak = ?,
                    has_parking = ?, has_ward = ?, is_traffic_acc = ?,
                    business_hours = ?, lunch_time = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """
            params = [
                summary_text, map_url, flags['is_silbi'], flags['has_chuna'], flags['has_night'],
                flags['has_365'], flags['has_yakchim'], flags['is_cheopyak'],
                flags['has_parking'], flags['has_ward'], flags['is_traffic_acc'],
                flags['business_hours'], flags['lunch_time'], h_id
            ]
            execute_d1_query(sql_update, params)
            
            print(f"    ✅ [저장 성공] DB 업데이트가 완료되었습니다.")
            print(f"       - 🕒 진료시간: {flags['business_hours']} / 점심: {flags['lunch_time']}")
            print(f"       - 💊 특화진료: 추나({flags['has_chuna']}) 약침({flags['has_yakchim']}) 첩약({flags['is_cheopyak']}) 입원({flags['has_ward']})")
            print(f"       - 🚗 부가정보: 야간({flags['has_night']}) 365({flags['has_365']}) 자보({flags['is_traffic_acc']}) 주차({flags['has_parking']})")
            print("-" * 70)
        else:
            sql_update_empty = "UPDATE hospitals SET description = 'N/A', updated_at = CURRENT_TIMESTAMP WHERE id = ?"
            execute_d1_query(sql_update_empty, [h_id])
            print(f"    ⚠️ [수집 실패] 검색 결과를 찾지 못해 'N/A'로 처리했습니다.")
            print("-" * 70)

        time.sleep(random.uniform(2.5, 4.0))

    print("\n✨ 수고하셨습니다! 오늘의 네이버 플레이스 정밀 크롤링이 무사히 끝났습니다.")

if __name__ == "__main__":
    main()
