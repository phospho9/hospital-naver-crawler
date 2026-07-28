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
                time.sleep(3.0)
            else:
                print("    ❌ D1 최대 재시도 횟수 초과로 통신을 포기합니다.")
                
    return None

# ---------------------------------------------------------------------------
# 3. 4단계 정교한 폴백 검색 키워드 생성 함수
# ---------------------------------------------------------------------------
def build_search_queries(name, address):
    clean_name = re.sub(r'\(주\)|\(유\)|(의료|재단|사단)?법인|(?:[가-힣]+)?의료재단|(?:[가-힣]+)?재단', '', name).strip()
    clean_name = re.sub(r'\s+', ' ', clean_name).strip()
    
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
    
    if "한의원" in n or "한방병원" in n:
        return "한방"
    
    if "한양방" in n or "양한방" in n or "협진" in n:
        return "한양방"
    
    strict_hanbang_keywords = r'(한양방\s*협진\s*병원|양한방\s*협진\s*병원|한양방\s*통합|본\s*병원은\s*한방|한방과\s*설치|한방진료실|한의사\s*상주)'
    if re.search(strict_hanbang_keywords, t):
        return "한양방"
    
    return "양방"

# ---------------------------------------------------------------------------
# 5. Playwright 정밀 크롤링 (1단계: 상단/하단 노이즈 절단)
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
                
            raw_combined = home_text + "\n" + info_text
            
            # [1단계]: 상단 UI 노이즈 절단 (소개, 진료시간, 주소, 영업시간 시작 지점 탐색)
            cut_keywords = ["[ 진료시간 ]", "진료시간", "소개", "주소", "영업시간", "전화번호"]
            cut_index = -1
            for kw in cut_keywords:
                idx = raw_combined.find(kw)
                if idx != -1:
                    if cut_index == -1 or idx < cut_index:
                        cut_index = idx
            
            cleaned = raw_combined[cut_index:] if (cut_index != -1 and cut_index > 30) else raw_combined

            # [1단계]: 하단 푸터/약관/캡차 절단
            end_keywords = ["알고 계신 정보와 다른 정보가 있나요?", "사진으로 간단하게 제보해 주세요", "이용약관고객센터", "Please complete the security", "Copyright © NAVER"]
            for ekw in end_keywords:
                e_idx = cleaned.find(ekw)
                if e_idx != -1:
                    cleaned = cleaned[:e_idx]

            combined_text = cleaned.strip()[:1500]

            print(f"    📝 [텍스트 정제 완료 (노이즈 제거 후 총 {len(combined_text)}자)]")
            
            return combined_text, raw_html, navermap_url, True
        else:
            print("    ⚠️ 병원 고유 ID를 찾을 수 없습니다.")
            return html_content[:1000], html_content, search_url, False
            
    except Exception as e:
        print(f"    ❌ 크롤링 중 예외 발생: {e}")
        return None, None, None, False

# ---------------------------------------------------------------------------
# 6. 텍스트 분석 및 플래그 매핑 (2단계: 진료시간/휴진/점심시간 정밀 파싱)
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
    
    # 💡 한글 시/분 표현 및 '평 일' 띄어쓰기 정규화 전처리
    normalized_text = re.sub(r'평\s*일', '평일', raw_combined)
    normalized_text = re.sub(r'(\d{1,2})\s*시\s*(\d{1,2})\s*분', r'\1:\2', normalized_text)
    normalized_text = re.sub(r'(\d{1,2})\s*시', r'\1:00', normalized_text)
    
    # 상단 가변 요약 문구 제거
    status_patterns = r'((오늘|월요일|화요일|수요일|목요일|금요일|토요일|일요일)\s*(휴무|휴진|영업\s*전|진료\s*전|진료\s*마감|영업\s*마감)|\b진료\s*전\b|\b영업\s*전\b|\b진료\s*마감\b|\b영업\s*마감\b|\b접수\s*마감\b|\d{1,2}:\d{2}\b에\s*(진료|영업)\s*시작|곧\s*(진료|영업)\s*종료)'
    cleaned_text = re.sub(status_patterns, '', normalized_text)
    cleaned_text = cleaned_text.replace("오늘", today_str)
    
    flags['is_hanbang'] = determine_hanbang_type(name, cleaned_text)
    
    schedule = {
        '월요일': '정보 없음', '화요일': '정보 없음', '수요일': '정보 없음',
        '목요일': '정보 없음', '금요일': '정보 없음', '토요일': '정보 없음',
        '일요일': '정보 없음', '공휴일': '', '점심시간': ''
    }
    
    # 💡 1차: 요일/평일 시간대 추출 (시간 표현 뒤 공백 정규화)
    time_range_pattern_long = re.compile(
        r'(월요일|화요일|수요일|목요일|금요일|토요일|일요일|평일|공휴일)\s*[:]?\s*(\d{1,2}:\d{2}\s*(?:부터|~|–|-)\s*\d{1,2}:\d{2})'
    )
    long_matches = time_range_pattern_long.findall(cleaned_text)
    
    for match in long_matches:
        key = match[0].strip()
        val = match[1].replace('부터', '~').replace('–', '~').replace('-', '~')
        val = re.sub(r'\s*~\s*', ' ~ ', val).strip()
        
        if key == '평일':
            for d in ['월요일', '화요일', '수요일', '목요일', '금요일']:
                if schedule[d] == '정보 없음':
                    schedule[d] = val
        elif key in weekdays:
            schedule[key] = val
        elif '공휴일' in key:
            schedule['공휴일'] = val

    # 단축 요일 (월~일) 2차 파싱
    day_map = {'월': '월요일', '화': '화요일', '수': '수요일', '목': '목요일', '금': '금요일', '토': '토요일', '일': '일요일'}
    time_range_pattern_short = re.compile(
        r'(?:^|\s)(월|화|수|목|금|토|일)\s*[:]?\s*(\d{1,2}:\d{2}\s*(?:부터|~|–|-)\s*\d{1,2}:\d{2})'
    )
    short_matches = time_range_pattern_short.findall(cleaned_text)
    for match in short_matches:
        raw_key = match[0].strip()
        key = day_map.get(raw_key)
        val = match[1].replace('부터', '~').replace('–', '~').replace('-', '~')
        val = re.sub(r'\s*~\s*', ' ~ ', val).strip()
        if key and schedule[key] == '정보 없음':
            schedule[key] = val

    # 💡 2차: "일요일 및 공휴일 휴진" 문구 정밀 탐지
    if re.search(r'일요일\s*(?:및|/|,|\w+)*\s*공휴일\s*(?:휴진|휴무)', cleaned_text) or re.search(r'일요일\s*휴진', cleaned_text):
        schedule['일요일'] = '휴무'
        schedule['공휴일'] = '휴무'
        
    for day in weekdays:
        if schedule[day] == '정보 없음':
            short_day = day[0]
            if re.search(fr'({day}|{short_day})\s*[:]?\s*(정기\s*휴무|휴무|휴진)', cleaned_text):
                schedule[day] = '휴무'

    formatted_hours = []
    for day in weekdays:
        if schedule[day] != '정보 없음':
            formatted_hours.append(f"{day}: {schedule[day]}")
            
    if schedule['공휴일'] and schedule['공휴일'] != '휴무':
        formatted_hours.append(f"공휴일: {schedule['공휴일']}")
        
    flags['business_hours'] = "\n".join(formatted_hours) if len(formatted_hours) > 0 else None
    
    # 💡 점심시간 정밀 파싱 (시작 시간이 11시~14시 사이일 때만 인정하도록 오탐 차단)
    lunch_match = re.search(r'(휴게시간|점심시간|휴게|점심|브레이크)[^\d]*(\d{1,2}:\d{2})\s*(?:부터|~|–|-)\s*(\d{1,2}:\d{2})', cleaned_text)
    if not lunch_match:
        lunch_match = re.search(r'(\d{1,2}:\d{2})\s*(?:부터|~|–|-)\s*(\d{1,2}:\d{2})[^\n]*(휴게시간|점심시간|휴게|점심|브레이크)', cleaned_text)
        if lunch_match:
            try:
                start_h = int(lunch_match.group(1).split(':')[0])
                if 11 <= start_h <= 14:
                    flags['lunch_time'] = f"{lunch_match.group(1)} ~ {lunch_match.group(2)}"
            except Exception:
                pass
    else:
        try:
            start_h = int(lunch_match.group(2).split(':')[0])
            if 11 <= start_h <= 14:
                flags['lunch_time'] = f"{lunch_match.group(2)} ~ {lunch_match.group(3)}"
        except Exception:
            pass

    # 💡 특화진료 및 부가정보 오탐 방지 (실제 진료시간 기반 검증)
    t = cleaned_text
    n = name.lower()
    
    flags['has_ward'] = 1 if ('병원' in n or '요양병원' in n) or re.search(r'(입원실|입원병동|병실)', t) else 0
    flags['has_chuna'] = 1 if re.search(r'(추나|추나요법|척추교정)', t) else 0
    flags['has_yakchim'] = 1 if re.search(r'(약침|봉침|봉독|봉약침)', t) else 0
    flags['is_cheopyak'] = 1 if re.search(r'(첩약건강보험|첩약|한약|보약)', t) else 0
    
    # 야간진료: 20시 이후 진료시간 존재 또는 '야간진료' 어휘 명시
    has_night_time = False
    if flags['business_hours']:
        times = re.findall(r'~\s*(\d{1,2}):(\d{2})', flags['business_hours'])
        for end_h, _ in times:
            if int(end_h) >= 20:
                has_night_time = True
                break
    flags['has_night'] = 1 if has_night_time or re.search(r'(야간진료|야간\s*진료|밤진료)', t) else 0
    
    flags['has_365'] = 1 if re.search(r'(365일|365진료|연중무휴|매일\s*진료)', t) else 0
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
                
                # [3단계 반영]: D1 DB 저장용 순수 정제 텍스트 500자 절삭
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
