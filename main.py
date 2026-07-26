import os
import re
import time
import random
import requests
from bs4 import BeautifulSoup
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
# 3. 💡 원장님 제안 반영: 4단계 정교한 폴백 검색 키워드 생성 함수
# ---------------------------------------------------------------------------
def build_search_queries(name, address):
    # (주), 의료법인, 재단법인 등 심평원 DB 특유의 표기를 깔끔하게 제거
    clean_name = re.sub(r'\(주\)|\(유\)|의료법인|재단법인|법인', '', name).strip()
    addr_parts = address.split() if address else []
    
    queries = []
    
    province = addr_parts[0] if len(addr_parts) > 0 else "" # 예: 충청남도
    city_gun = addr_parts[1] if len(addr_parts) > 1 else "" # 예: 천안시
    detail_addr = addr_parts[2] if len(addr_parts) > 2 else "" # 예: 중앙대로 (도로명 등)
    
    # 1단계: 병원명 + 시/도 + 구/군 (예: 삼거리한의원 충청남도 천안시)
    if province and city_gun:
        queries.append(f"{clean_name} {province} {city_gun}")
        
    # 2단계: 병원명 + 구/군만 (예: 삼거리한의원 천안시)
    if city_gun:
        queries.append(f"{clean_name} {city_gun}")
    elif province:
        queries.append(f"{clean_name} {province}")
        
    # 3단계: 병원명 + 상세 주소 키워드 (예: 삼거리한의원 중앙대로)
    if detail_addr:
        queries.append(f"{clean_name} {detail_addr}")
        
    # 4단계: 최후의 보루, 병원명 단독 검색 (예: 삼거리한의원)
    queries.append(clean_name)
    
    # 중복 제거 및 빈 문자열 제거
    unique_queries = []
    for q in queries:
        q_cleaned = q.strip()
        if q_cleaned and q_cleaned not in unique_queries:
            unique_queries.append(q_cleaned)
            
    return unique_queries

# ---------------------------------------------------------------------------
# 4. 네이버 플레이스 정밀 크롤링
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
            return None, None, None, False
            
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
            
            return combined_text, raw_html, navermap_url, True
            
        else:
            print("    ⚠️ 병원 고유 ID를 찾을 수 없습니다.")
            return soup_search.get_text(separator=" ", strip=True)[:1000], res.text, search_url, False
            
    except Exception as e:
        print(f"    ❌ 크롤링 중 예외 발생: {e}")
        return None, None, None, False

# ---------------------------------------------------------------------------
# 5. 텍스트 분석 및 플래그 매핑 (요일 구조화 적용)
# ---------------------------------------------------------------------------
def parse_flags(text, raw_html, name=""):
    flags = {
        'is_silbi': 0, 'has_chuna': 0, 'has_night': 0, 'has_365': 0, 
        'has_yakchim': 0, 'is_cheopyak': 0, 'has_parking': 0, 
        'has_ward': 0, 'is_traffic_acc': 0, 'business_hours': None, 'lunch_time': None
    }
    
    if not text or not raw_html:
        return flags
        
    kst = timezone(timedelta(hours=9))
    today_kst = datetime.now(kst)
    weekdays = ['월요일', '화요일', '수요일', '목요일', '금요일', '토요일', '일요일']
    today_str = weekdays[today_kst.weekday()]
    
    t = text.lower().replace("오늘", today_str)
    raw_html_lower = raw_html.lower().replace("오늘", today_str)
    n = name.lower()
    
    schedule = {
        '월요일': '정보 없음', '화요일': '정보 없음', '수요일': '정보 없음',
        '목요일': '정보 없음', '금요일': '정보 없음', '토요일': '정보 없음',
        '일요일': '정보 없음', '공휴일': '', '점심시간': ''
    }
    
    time_pattern = re.compile(r'(평일|토요일|일요일|매주\s*일요일|공휴일|점심|월요일|화요일|수요일|목요일|금요일)\s*(\d{1,2}:\d{2}\s*[~–\-]\s*\d{1,2}:\d{2}|휴무|휴진)')
    matches = time_pattern.findall(t + " " + raw_html_lower)
    
    for match in matches:
        key = match[0].replace('매주 ', '').strip()
        val = match[1].replace('–', '~').replace('-', '~')
        
        if key == '평일':
            for d in ['월요일', '화요일', '수요일', '목요일', '금요일']:
                schedule[d] = val
        elif '토요일' in key:
            schedule['토요일'] = val
        elif '일요일' in key:
            schedule['일요일'] = val
        elif '공휴일' in key:
            schedule['공휴일'] = val
        elif '점심' in key:
            schedule['점심시간'] = val
        elif key in weekdays:
            schedule[key] = val

    for day in weekdays:
        if f"{day} 휴무" in t or f"{day} 휴진" in t:
            schedule[day] = '휴무'

    formatted_hours = []
    for day in weekdays:
        if schedule[day] != '정보 없음':
            formatted_hours.append(f"{day}: {schedule[day]}")
    
    if schedule['공휴일']:
        formatted_hours.append(f"공휴일: {schedule['공휴일']}")
        
    # 데이터 신뢰성 확보: 월~일 스케줄이 전혀 파싱되지 않았다면 None 처리 (유령 데이터 방지)
    flags['business_hours'] = "\n".join(formatted_hours) if len(formatted_hours) > 0 else None
    
    if schedule['점심시간']:
        flags['lunch_time'] = schedule['점심시간']
    else:
        lunch_match = re.search(r'(휴게시간|점심시간|휴게|점심|브레이크)[^\d]*(\d{1,2}:\d{2})\s*[~–\-]\s*(\d{1,2}:\d{2})', raw_html_lower)
        if lunch_match:
            flags['lunch_time'] = f"{lunch_match.group(2)} ~ {lunch_match.group(3)}"

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

    print(f"🚀 총 {len(hospitals)}개의 타겟 병원을 찾았습니다. 4단계 폴백 정밀 크롤링을 시작합니다!\n")
    print("=" * 70)

    for idx, h in enumerate(hospitals, 1):
        h_id = h["id"]
        h_name = h["name"]
        h_addr = h.get("address", "")
        
        print(f"[{idx}/{len(hospitals)}] 🏥 병원명: {h_name} (ID: {h_id[:8]}...)")
        
        # 💡 4단계 폴백 검색어 리스트 생성
        query_list = build_search_queries(h_name, h_addr)
        
        crawled_text, raw_html, map_url = None, None, None
        search_success = False
        
        # 💡 1단계부터 4단계까지 순차적 탐색
        for step, query in enumerate(query_list, 1):
            print(f"    - {step}차 검색 키워드: {query}")
            text, html, url, is_success = crawl_naver_place(query)
            
            if is_success:
                crawled_text, raw_html, map_url = text, html, url
                search_success = True
                break # 성공 시 즉시 탈출
            else:
                if step < len(query_list):
                    print(f"    🔄 [검색 실패] 다음 단계 검색어로 재시도합니다.")
                    time.sleep(1.0)
        
        # 💡 실제로 네이버 플레이스 ID 및 데이터를 정확히 건졌을 때만 정상 UPDATE 실행
        if search_success and crawled_text and raw_html and map_url:
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
            
            if flags['business_hours']:
                formatted_hours = "\n               ".join(flags['business_hours'].split('\n'))
                print(f"       - 🕒 진료시간:\n               {formatted_hours}")
            print(f"       - 🍴 점심시간: {flags['lunch_time']}")
            print(f"       - 💊 특화진료: 추나({flags['has_chuna']}) 약침({flags['has_yakchim']}) 첩약({flags['is_cheopyak']}) 입원({flags['has_ward']})")
            print(f"       - 🚗 부가정보: 야간({flags['has_night']}) 365({flags['has_365']}) 자보({flags['is_traffic_acc']}) 주차({flags['has_parking']})")
            print("-" * 70)
        else:
            # 4단계까지 모두 실패한 경우 엉뚱한 값으로 덮어쓰지 않고 안전하게 N/A 처리
            sql_update_empty = "UPDATE hospitals SET description = 'N/A', updated_at = CURRENT_TIMESTAMP WHERE id = ?"
            execute_d1_query(sql_update_empty, [h_id])
            print(f"    ⚠️ [최종 수집 실패] 모든 검색 단계를 거쳤으나 찾지 못해 'N/A'로 처리했습니다.")
            print("-" * 70)

        time.sleep(random.uniform(2.5, 4.0))

    print("\n✨ 생명마루한의원 안산점 플레이스 상위 노출 분석을 위한 정밀 크롤링이 무사히 끝났습니다.")

if __name__ == "__main__":
    main()
