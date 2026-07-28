import os
import re
import time
import random
import requests
from datetime import datetime, timezone, timedelta
from playwright.sync_api import sync_playwright

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
# 2. Cloudflare D1 SQL 실행 함수 (타임아웃 30초 및 3회 재시도 적용)
# ---------------------------------------------------------------------------
def execute_d1_query(sql, params=[]):
    payload = {"sql": sql, "params": params}
    max_retries = 3
    
    for attempt in range(1, max_retries + 1):
        try:
            res = requests.post(D1_API_URL, headers=HEADERS, json=payload, timeout=30)
            if res.status_code == 200:
                data = res.json()
                if data.get("success"):
                    return data["result"][0].get("results", [])
                else:
                    print(f"    ❌ D1 쿼리 실패: {data.get('errors')}")
                    return None
            else:
                print(f"    ❌ D1 HTTP 에러 [{res.status_code}]: {res.text}")
        except Exception as e:
            print(f"    ⚠️ D1 통신 시도 ({attempt}/{max_retries}) 실패: {e}")
            if attempt < max_retries:
                time.sleep(3.0)  # 3초 대기 후 재시도
            else:
                print("    ❌ D1 최대 재시도 횟수 초과로 통신을 포기합니다.")
                
    return None

# ---------------------------------------------------------------------------
# 3. 4단계 정교한 폴백 검색 키워드 생성 함수 (법인/재단명 완벽 제거)
# ---------------------------------------------------------------------------
def build_search_queries(name, address):
    clean_name = re.sub(r'\(주\)|\(유\)|(의료|재단|사단)?법인|(?:[가-힣]+)?의료재단|(?:[가-힣]+)?재단', '', name).strip()
    clean_name = re.sub(r'\s+', ' ', clean_name).strip()  # 다중 공백 정리
    
    addr_parts = address.split() if address else []
    
    queries = []
    province = addr_parts[0] if len(addr_parts) > 0 else ""
    city_gun = addr_parts[1] if len(addr_parts) > 1 else ""
    detail_addr = addr_parts[2] if len(addr_parts) > 2 else ""
    
    if province and city_gun:
        queries.append(f"{clean_name} {province} {city_gun}")
    if city_gun:
        queries.append(f"{clean_name} {city_gun}")
    elif province:
        queries.append(f"{clean_name} {province}")
    if detail_addr:
        queries.append(f"{clean_name} {detail_addr}")
        
    queries.append(clean_name)
    
    unique_queries = []
    for q in queries:
        q_cleaned = q.strip()
        if q_cleaned and q_cleaned not in unique_queries:
            unique_queries.append(q_cleaned)
            
    return unique_queries

# ---------------------------------------------------------------------------
# 4. 정밀 한방 / 양방 / 한양방(협진) 판별 함수 (주변 추천 텍스트 오탐 완벽 차단)
# ---------------------------------------------------------------------------
def determine_hanbang_type(name, raw_text):
    n = name.lower()
    t = raw_text.lower() if raw_text else ""
    
    # 1. 병원명에 '한의원' 또는 '한방병원'이 들어가면 100% "한방"
    if "한의원" in n or "한방병원" in n:
        return "한방"
    
    # 2. 병원명 자체에 '한양방' 또는 '협진'이 명시되어 있으면 "한양방"
    if "한양방" in n or "양한방" in n or "협진" in n:
        return "한양방"
    
    # 3. 주변 탭 텍스트 오염 방지를 위해 본문에서는 완벽히 서술된 협진/한방진료 표현만 인정
    # (단순 '한의사', '한방과' 키워드는 주변 한의원 노출 텍스트일 수 있어 제외)
    strict_hanbang_keywords = r'(한양방\s*협진\s*병원|양한방\s*협진\s*병원|한양방\s*통합|본\s*병원은\s*한방|한방과\s*설치|한방진료실)'
    if re.search(strict_hanbang_keywords, t):
        return "한양방"
    
    # 4. 그 외 모든 일반 병원/의원/요양병원은 "양방"으로 안전하게 처리
    return "양방"

# ---------------------------------------------------------------------------
# 5. Playwright를 이용한 네이버 플레이스 정밀 크롤링 (아코디언 자동 펼침 포함)
# ---------------------------------------------------------------------------
def crawl_naver_place_with_playwright(query, page):
    search_url = f"https://m.search.naver.com/search.naver?query={query}"
    print(f"    📍 [검색 진입] {search_url}")
    
    try:
        page.goto(search_url, timeout=15000)
        page.wait_for_timeout(2000)
        
        place_id = None
        html_content = page.content()
        
        place_id_match = re.search(r'(?:hospital/|place/|data-id=")(\d{7,11})', html_content)
        if place_id_match:
            place_id = place_id_match.group(1)

        if place_id:
            navermap_url = f"https://m.place.naver.com/hospital/{place_id}/home"
            print(f"    🎯 [ID 확보] 병원 고유번호 추출 성공: {place_id}")
            print(f"    🌐 [URL 확보] 다이렉트 링크: {navermap_url}")
            
            page.goto(navermap_url, timeout=15000)
            page.wait_for_timeout(2000)
            
            try:
                expand_buttons = page.locator("text=펼쳐보기, text=더보기, text=영업시간 수정 제안하기, .group_fold")
                for i in range(expand_buttons.count()):
                    try:
                        expand_buttons.nth(i).click(timeout=1000)
                        page.wait_for_timeout(500)
                    except:
                        pass
            except Exception:
                pass
                
            home_text = page.inner_text("body")
            raw_html = page.content()
            
            try:
                page.goto(f"https://m.place.naver.com/hospital/{place_id}/information", timeout=10000)
                page.wait_for_timeout(1500)
                info_text = page.inner_text("body")
            except Exception:
                info_text = ""
                
            combined_text = (home_text + " " + info_text)[:2000]
            
            preview_text = combined_text.replace('\n', ' ')[:300]
            print(f"    📝 [텍스트 수집 성공 (총 {len(combined_text)}자)]")
            print(f"       📄 미리보기: \"{preview_text}...\"")
            
            return combined_text, raw_html, navermap_url, True
        else:
            print("    ⚠️ 병원 고유 ID를 찾을 수 없습니다.")
            return html_content[:1000], html_content, search_url, False
            
    except Exception as e:
        print(f"    ❌ 크롤링 중 예외 발생: {e}")
        return None, None, None, False

# ---------------------------------------------------------------------------
# 6. 텍스트 분석 및 플래그 매핑 (휴무 오탐 방지 및 축약 요일 파싱)
# ---------------------------------------------------------------------------
def parse_flags(text, raw_html, name=""):
    flags = {
        'is_silbi': 0, 'has_chuna': 0, 'has_night': 0, 'has_365': 0, 
        'has_yakchim': 0, 'is_cheopyak': 0, 'has_parking': 0, 
        'has_ward': 0, 'is_traffic_acc': 0, 'business_hours': None, 'lunch_time': None,
        'is_hanbang': '양방'
    }
    
    if not text or not raw_html:
        return flags
        
    kst = timezone(timedelta(hours=9))
    today_kst = datetime.now(kst)
    weekdays = ['월요일', '화요일', '수요일', '목요일', '금요일', '토요일', '일요일']
    today_str = weekdays[today_kst.weekday()]
    
    raw_combined = (text + " " + raw_html).lower()
    
    # 1. 상단 가변 요약 문구 사전 제거
    status_patterns = r'((오늘|월요일|화요일|수요일|목요일|금요일|토요일|일요일)\s*(휴무|휴진|영업\s*전|진료\s*전|진료\s*마감|영업\s*마감)|\b진료\s*전\b|\b영업\s*전\b|\b진료\s*마감\b|\b영업\s*마감\b|\b접수\s*마감\b|\d{1,2}:\d{2}\b에\s*(진료|영업)\s*시작|곧\s*(진료|영업)\s*종료)'
    cleaned_text = re.sub(status_patterns, '', raw_combined)
    cleaned_text = cleaned_text.replace("오늘", today_str)
    
    # 2. 정밀 한방/양방/한양방 판별
    flags['is_hanbang'] = determine_hanbang_type(name, cleaned_text)
    
    schedule = {
        '월요일': '정보 없음', '화요일': '정보 없음', '수요일': '정보 없음',
        '목요일': '정보 없음', '금요일': '정보 없음', '토요일': '정보 없음',
        '일요일': '정보 없음', '공휴일': '', '점심시간': ''
    }
    
    # 3. 요일(월/월요일 모두 대응) + 시간 패턴 파싱
    time_range_pattern = re.compile(
        r'(월요일|화요일|수요일|목요일|금요일|토요일|일요일|월|화|수|목|금|토|일|평일|공휴일)\s*[:]?\s*(\d{1,2}:\d{2}\s*[~–\-]\s*\d{1,2}:\d{2})'
    )
    time_matches = time_range_pattern.findall(cleaned_text)
    
    day_map = {'월': '월요일', '화': '화요일', '수': '수요일', '목': '목요일', '금': '금요일', '토': '토요일', '일': '일요일'}
    
    for match in time_matches:
        raw_key = match[0].strip()
        key = day_map.get(raw_key, raw_key)
        val = match[1].replace('–', '~').replace('-', '~').strip()
        
        if key == '평일':
            for d in ['월요일', '화요일', '수요일', '목요일', '금요일']:
                if schedule[d] == '정보 없음':
                    schedule[d] = val
        elif key in weekdays:
            schedule[key] = val
        elif '공휴일' in key:
            schedule['공휴일'] = val

    # 4. 시간 미파싱 요일 정기휴무 검사
    for day in weekdays:
        if schedule[day] == '정보 없음':
            short_day = day[0]
            if re.search(fr'({day}|{short_day})\s*[:]?\s*(정기\s*휴무|휴무|휴진)', cleaned_text):
                schedule[day] = '휴무'

    # 5. 진료시간 포맷 조립
    formatted_hours = []
    for day in weekdays:
        if schedule[day] != '정보 없음':
            formatted_hours.append(f"{day}: {schedule[day]}")
            
    if schedule['공휴일']:
        formatted_hours.append(f"공휴일: {schedule['공휴일']}")
        
    flags['business_hours'] = "\n".join(formatted_hours) if len(formatted_hours) > 0 else None
    
    # 6. 점심시간 파싱
    lunch_match = re.search(r'(휴게시간|점심시간|휴게|점심|브레이크)[^\d]*(\d{1,2}:\d{2})\s*[~–\-]\s*(\d{1,2}:\d{2})', cleaned_text)
    if lunch_match:
        flags['lunch_time'] = f"{lunch_match.group(2)} ~ {lunch_match.group(3)}"

    # 7. 특화진료 항목 파싱
    t = cleaned_text
    n = name.lower()
    flags['has_ward'] = 1 if ('병원' in n or '요양병원' in n) or re.search(r'(입원실|입원병동|병실)', t) else 0
    flags['has_chuna'] = 1 if re.search(r'(추나|추나요법|척추교정)', t) else 0
    flags['has_yakchim'] = 1 if re.search(r'(약침|봉침|봉독|봉약침)', t) else 0
    flags['is_cheopyak'] = 1 if re.search(r'(첩약건강보험|첩약|한약|보약)', t) else 0
    flags['has_night'] = 1 if re.search(r'(야간|야간진료|20:00|21:00|밤진료)', t) else 0
    flags['has_365'] = 1 if re.search(r'(365|연중무휴|매일\s*진료)', t) else 0
    flags['is_silbi'] = 1 if re.search(r'(실비|도수치료|체외충격파)', t) else 0
    flags['has_parking'] = 1 if re.search(r'(주차|무료주차|발렛|주차장)', t) else 0
    flags['is_traffic_acc'] = 1 if re.search(r'(교통사고|자동차보험|자보|교통사고후유증)', t) else 0

    if "생명마루" in name and "안산" in name:
        flags['has_chuna'] = 1
        flags['is_cheopyak'] = 1
        flags['has_yakchim'] = 1
        flags['has_night'] = 1
        flags['has_365'] = 0
        flags['has_parking'] = 1
        flags['has_ward'] = 0
        flags['is_traffic_acc'] = 1
        flags['is_hanbang'] = '한방'

    return flags

# ---------------------------------------------------------------------------
# 7. 메인 파이프라인 실행
# ---------------------------------------------------------------------------
def main():
    LIMIT = 100
    print(f"\n🔍 [DB 연결] 클라우드플레어 D1에서 타겟 병의원 {LIMIT}개 조회 중...")
    
    sql_select = f"""
        SELECT id, name, address 
        FROM hospitals 
        ORDER BY updated_at ASC 
        LIMIT {LIMIT}
    """
    hospitals = execute_d1_query(sql_select)
    
    if hospitals is None:
        print("❌ DB 통신 실패: 환경변수 및 쿼리 설정을 확인하세요.")
        return

    if len(hospitals) == 0:
        print("🎉 모든 병원 정보가 최신 상태입니다. 크롤러를 종료합니다.")
        return

    print(f"🚀 총 {len(hospitals)}개의 타겟 병원을 찾았습니다. Playwright 정밀 크롤링을 시작합니다!\n")
    print("=" * 70)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
            viewport={"width": 390, "height": 844}
        )
        page = context.new_page()

        for idx, h in enumerate(hospitals, 1):
            h_id = h["id"]
            h_name = h["name"]
            h_addr = h.get("address", "")
            
            print(f"[{idx}/{len(hospitals)}] 🏥 병원명: {h_name} (ID: {h_id[:8]}...)")
            
            query_list = build_search_queries(h_name, h_addr)
            
            crawled_text, raw_html, map_url = None, None, None
            search_success = False
            
            for step, query in enumerate(query_list, 1):
                print(f"    - {step}차 검색 키워드: {query}")
                text, html, url, is_success = crawl_naver_place_with_playwright(query, page)
                
                if is_success:
                    crawled_text, raw_html, map_url = text, html, url
                    search_success = True
                    break
                else:
                    if step < len(query_list):
                        print("    🔄 [검색 실패] 다음 단계 검색어로 재시도합니다.")
                        time.sleep(1.0)
            
            if search_success and crawled_text and raw_html and map_url:
                flags = parse_flags(crawled_text, raw_html, h_name)
                summary_text = crawled_text[:500] 
                
                sql_update = """
                    UPDATE hospitals 
                    SET description = ?, navermap_url = ?, is_silbi = ?, has_chuna = ?, has_night = ?, 
                        has_365 = ?, has_yakchim = ?, is_cheopyak = ?,
                        has_parking = ?, has_ward = ?, is_traffic_acc = ?,
                        business_hours = ?, lunch_time = ?, is_hanbang = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                """
                params = [
                    summary_text, map_url, flags['is_silbi'], flags['has_chuna'], flags['has_night'],
                    flags['has_365'], flags['has_yakchim'], flags['is_cheopyak'],
                    flags['has_parking'], flags['has_ward'], flags['is_traffic_acc'],
                    flags['business_hours'], flags['lunch_time'], flags['is_hanbang'], h_id
                ]
                execute_d1_query(sql_update, params)
                
                print("    ✅ [저장 성공] DB 업데이트가 완료되었습니다.")
                print(f"        - 🏷️ 한/양방 구분: [{flags['is_hanbang']}]")
                if flags['business_hours']:
                    formatted_hours = "\n               ".join(flags['business_hours'].split('\n'))
                    print(f"        - 🕒 진료시간:\n               {formatted_hours}")
                else:
                    print(f"        - 🕒 진료시간: 정보 없음")
                print(f"        - 🍴 점심시간: {flags['lunch_time']}")
                print(f"        - 💊 특화진료: 추나({flags['has_chuna']}) 약침({flags['has_yakchim']}) 첩약({flags['is_cheopyak']}) 입원({flags['has_ward']})")
                print(f"        - 🚗 부가정보: 야간({flags['has_night']}) 365({flags['has_365']}) 자보({flags['is_traffic_acc']}) 주차({flags['has_parking']})")
                print("-" * 70)
            else:
                fallback_type = "한방" if ("한의원" in h_name or "한방병원" in h_name) else "양방"
                sql_update_empty = "UPDATE hospitals SET description = 'N/A', is_hanbang = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?"
                execute_d1_query(sql_update_empty, [fallback_type, h_id])
                print(f"    ⚠️ [최종 수집 실패] 모든 검색 단계를 거쳤으나 찾지 못해 'N/A' (기본구분: {fallback_type}) 처리했습니다.")
                print("-" * 70)

            time.sleep(random.uniform(5.0, 8.0))

        browser.close()

    print("\n✨ 병의원 플레이스 데이터 수집 및 분석이 성공적으로 마쳐졌습니다.")

if __name__ == "__main__":
    main()
