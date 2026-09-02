import os
import re
import time
import json
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
                print(f"      ❌ [D1 에러] 쿼리 실패: {data.get('errors')}")
        else:
            print(f"      ❌ [D1 에러] HTTP [{res.status_code}]: {res.text}")
    except Exception as e:
        print(f"      ❌ [D1 예외] DB 통신 중 에러 발생: {e}")
    return None

# ---------------------------------------------------------------------------
# 3. 검색 쿼리 빌더 & 한/양방 구분
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

def determine_hanbang_type(name, raw_text):
    n = name.lower()
    t = raw_text.lower() if raw_text else ""
    if "한의원" in n or "한방병원" in n:
        return "한방"
    cooperation_keywords = r'(한양방|한·양방|양한방|한양방협진|한·양방협진|양·한방협진|협진병원|협진진료|의사·한의사|한의사·의사)'
    if re.search(cooperation_keywords, t) or re.search(cooperation_keywords, n):
        return "한양방"
    return "양방"

# ---------------------------------------------------------------------------
# 4. 네이버 구조화 JSON (__APOLLO_STATE__) 정밀 파서
# ---------------------------------------------------------------------------
def parse_from_apollo_state(raw_html, name):
    apollo_match = re.search(r'window\.__APOLLO_STATE__\s*=\s*(\{.*?\});<\/script>', raw_html, re.DOTALL)
    if not apollo_match:
        return None

    try:
        data = json.loads(apollo_match.group(1))
    except Exception:
        return None

    target_place = None
    for key, val in data.items():
        if isinstance(val, dict):
            if any(k in key for k in ["HospitalBase", "Hospital:", "PlaceBase", "Place:"]):
                if "id" in val or "name" in val:
                    target_place = val
                    break

    if not target_place:
        for key, val in data.items():
            if isinstance(val, dict) and "name" in val and ("businessHours" in val or "description" in val):
                target_place = val
                break

    if not target_place:
        return None

    flags = {
        'description': None,
        'business_hours': None,
        'lunch_time': None,
        'conveniences': []
    }

    # 1. 소개글 정밀 추출 (병원 공식 작성글 우선)
    desc = target_place.get("description") or target_place.get("microReview") or target_place.get("introduction")
    if desc:
        desc = re.sub(r'<[^>]+>', ' ', str(desc))
        desc = re.sub(r'\s+', ' ', desc).strip()
        if len(desc) > 5:
            flags['description'] = desc

    # 2. 편의시설(주차 등) 정밀 추출
    conveniences = target_place.get("conveniences") or target_place.get("facilityInfo") or []
    if isinstance(conveniences, list):
        flags['conveniences'] = [str(c) for c in conveniences]

    # 3. 진료시간 및 휴게(점심)시간 구조화 파싱
    weekdays_order = ['월요일', '화요일', '수요일', '목요일', '금요일', '토요일', '일요일', '공휴일']
    parsed_hours = {}
    
    # Apollo 스토어 내부의 BusinessHour 검색
    for k, v in data.items():
        if isinstance(v, dict) and ("BusinessHour" in k or ("day" in v and "businessHours" in v)):
            day = v.get("day")
            if not day:
                continue
            day_kor = f"{day}요일" if not day.endswith("요일") and day != "공휴일" else day
            
            is_day_off = v.get("isDayOff", False)
            if is_day_off:
                parsed_hours[day_kor] = "정기휴무"
            else:
                start = v.get("startTime") or v.get("start") or ""
                end = v.get("endTime") or v.get("end") or ""
                desc_time = v.get("description") or ""
                if start and end:
                    parsed_hours[day_kor] = f"{start} ~ {end}"
                elif desc_time:
                    parsed_hours[day_kor] = desc_time

            # 점심시간 / 브레이크타임
            break_time = v.get("breakTime") or v.get("breakStartTime")
            if break_time and not flags['lunch_time']:
                if isinstance(break_time, str):
                    flags['lunch_time'] = break_time
                elif isinstance(break_time, dict):
                    b_start = break_time.get("startTime", "")
                    b_end = break_time.get("endTime", "")
                    if b_start and b_end:
                        flags['lunch_time'] = f"{b_start} ~ {b_end}"

    if parsed_hours:
        ordered_lines = []
        for d in weekdays_order:
            if d in parsed_hours:
                ordered_lines.append(f"{d}: {parsed_hours[d]}")
        for d, val in parsed_hours.items():
            if d not in weekdays_order:
                ordered_lines.append(f"{d}: {val}")
        if ordered_lines:
            flags['business_hours'] = "\n".join(ordered_lines)

    return flags

# ---------------------------------------------------------------------------
# 5. DOM 텍스트 정밀 2차 파서 (폴백 전용)
# ---------------------------------------------------------------------------
def parse_from_text_fallback(text, raw_html, name=""):
    flags = {'description': None, 'business_hours': None, 'lunch_time': None}
    if not text:
        return flags

    # 소개글 추출: '소개' 또는 '찾아가는길' 블록만 한정 발췌
    intro_match = re.search(r'(?:병원소개|소개|찾아가는길)\s*(.*?)(?:영업시간|진료시간|휴무일|편의|전화번호|홈\s*리뷰|블로그|제보)', text, re.DOTALL)
    if intro_match:
        clean_desc = intro_match.group(1).strip()
        clean_desc = re.sub(r'(거리뷰|지도|내비게이션|홈|리뷰|사진|주변\s*정보|전화|공유|길찾기|고유가).*', '', clean_desc).strip()
        if len(clean_desc) > 3:
            flags['description'] = clean_desc

    # 요일별 진료시간 파싱
    weekdays = ['월요일', '화요일', '수요일', '목요일', '금요일', '토요일', '일요일']
    schedule = {}
    time_pattern = re.compile(
        r'(월|화|수|목|금|토|일)(?:요일)?\s*[:]?\s*'
        r'(\d{1,2}:\d{2}\s*[~–\-]\s*\d{1,2}:\d{2}|휴무|휴진|정기휴무)'
    )
    for m in time_pattern.finditer(text):
        day = f"{m.group(1)}요일"
        val = m.group(2).replace('–', '~').replace('-', '~').strip()
        if day not in schedule:
            schedule[day] = val

    formatted = [f"{d}: {schedule[d]}" for d in weekdays if d in schedule]
    if formatted:
        flags['business_hours'] = "\n".join(formatted)

    # 점심시간 (11시~14시 시작하는 휴게시간만 인정)
    lunch_match = re.search(r'(?:휴게시간|점심시간|휴게|브레이크\s*타임)\s*[:]?\s*(\d{1,2}:\d{2})\s*[~–\-]\s*(\d{1,2}:\d{2})', text)
    if lunch_match:
        sh, eh = lunch_match.group(1), lunch_match.group(2)
        try:
            if 11 <= int(sh.split(':')[0]) <= 14:
                flags['lunch_time'] = f"{sh} ~ {eh}"
        except Exception:
            pass

    return flags

# ---------------------------------------------------------------------------
# 6. 종합 플래그 병합 파서
# ---------------------------------------------------------------------------
def parse_flags(text, raw_html, name=""):
    flags = {
        'is_silbi': 0, 'has_chuna': 0, 'has_night': 0, 'has_365': 0, 
        'has_yakchim': 0, 'is_cheopyak': 0, 'has_parking': 0, 
        'has_ward': 0, 'is_traffic_acc': 0, 'business_hours': None, 'lunch_time': None,
        'description': None,
        'is_hanbang': '양방'
    }
    if not text or not raw_html:
        return flags

    t = text.lower()
    n = name.lower()
    combined_corpus = t + " " + raw_html.lower()

    flags['is_hanbang'] = determine_hanbang_type(name, combined_corpus)

    # 1. 1차 시도: Apollo JSON 고도화 파싱
    apollo_res = parse_from_apollo_state(raw_html, name)
    conveniences_str = ""
    if apollo_res:
        flags['description'] = apollo_res.get('description')
        flags['business_hours'] = apollo_res.get('business_hours')
        flags['lunch_time'] = apollo_res.get('lunch_time')
        conveniences_str = " ".join(apollo_res.get('conveniences', [])).lower()

    # 2. 2차 시도: 누락된 데이터는 DOM 텍스트 파서로 보완
    fallback_res = parse_from_text_fallback(text, raw_html, name)
    if not flags['description']:
        flags['description'] = fallback_res.get('description')
    if not flags['business_hours']:
        flags['business_hours'] = fallback_res.get('business_hours')
    if not flags['lunch_time']:
        flags['lunch_time'] = fallback_res.get('lunch_time')

    # 3. 특화 진료 / 시설 배지 정밀 판별
    all_context = combined_corpus + " " + conveniences_str

    flags['has_ward'] = 1 if ('병원' in n or '요양병원' in n) or re.search(r'(입원실|입원병동|병실)', all_context) else 0
    flags['has_chuna'] = 1 if re.search(r'(추나|추나요법|척추교정)', all_context) else 0
    flags['has_yakchim'] = 1 if re.search(r'(약침|봉침|봉독|봉약침)', all_context) else 0
    flags['is_cheopyak'] = 1 if re.search(r'(첩약건강보험|첩약|한약|보약)', all_context) else 0
    flags['has_night'] = 1 if re.search(r'(야간|야간진료|20:00|21:00|밤진료)', all_context) else 0
    flags['has_365'] = 1 if re.search(r'(365|연중무휴|매일\s*진료)', all_context) else 0
    flags['is_silbi'] = 1 if re.search(r'(실비|도수치료|체외충격파)', all_context) else 0
    flags['has_parking'] = 1 if ('주차' in conveniences_str or re.search(r'(주차|무료주차|발렛|주차장)', all_context)) else 0
    flags['is_traffic_acc'] = 1 if re.search(r'(교통사고|자동차보험|자보|교통사고후유증)', all_context) else 0

    return flags

# ---------------------------------------------------------------------------
# 7. Playwright 수집 엔진
# ---------------------------------------------------------------------------
def crawl_naver_place_with_playwright(query, page):
    search_url = f"https://m.search.naver.com/search.naver?query={query}"
    print(f"    📍 [검색 시도] {query}")
    
    try:
        page.goto(search_url, timeout=15000, wait_until="domcontentloaded")
        page.wait_for_timeout(random.randint(800, 1400))
        
        place_id = None
        html_content = page.content()
        
        place_id_match = re.search(r'(?:hospital/|place/|data-id=")(\d{7,11})', html_content)
        if place_id_match:
            place_id = place_id_match.group(1)
            print(f"      🔹 [ID 탐지] 네이버 고유 ID 확보 ({place_id})")
        else:
            print("      🔸 [ID 누락] 검색 결과에서 고유 ID 미확인")

        if place_id:
            navermap_url = f"https://m.place.naver.com/hospital/{place_id}/home"
            page.goto(navermap_url, timeout=15000, wait_until="domcontentloaded")
            page.wait_for_timeout(random.randint(1200, 1800))
            
            # 정보 펼쳐보기 탭 자동 확장
            try:
                click_selectors = ["text=펼쳐보기", "text=영업시간", ".g2u4Z", ".group_fold", "[aria-expanded='false']"]
                for selector in click_selectors:
                    try:
                        elements = page.locator(selector)
                        for i in range(min(elements.count(), 2)):
                            elements.nth(i).click(timeout=800)
                            page.wait_for_timeout(300)
                    except Exception:
                        pass
            except Exception:
                pass
                
            home_text = page.inner_text("body")
            home_html = page.content()
            
            # 상세 정보 탭 탐색 (정보 탭)
            info_text = ""
            try:
                page.goto(f"https://m.place.naver.com/hospital/{place_id}/information", timeout=12000, wait_until="domcontentloaded")
                page.wait_for_timeout(random.randint(800, 1200))
                info_text = page.inner_text("body")
            except Exception:
                pass
                
            raw_text = home_text + "\n" + info_text
            return raw_text, home_html, navermap_url, True
        else:
            return None, html_content, search_url, False
            
    except Exception as e:
        short_err = str(e).split('Call log:')[0].strip()
        print(f"      ❌ [접속 에러] {short_err}")
        return None, None, None, False

# ---------------------------------------------------------------------------
# 8. 메인 파이프라인 실행 루프
# ---------------------------------------------------------------------------
def main():
    LIMIT = 50
    print(f"\n🔍 [DB 연결] 클라우드플레어 D1에서 타겟 병의원 {LIMIT}개 조회 중...")
    
    sql_select = f"""
        SELECT id, name, address 
        FROM hospitals 
        WHERE (description IS NULL OR updated_at < datetime('now', '-30 days')) 
        ORDER BY updated_at ASC LIMIT {LIMIT}
    """
    hospitals = execute_d1_query(sql_select)
    
    if not hospitals:
        print("🎉 모든 병원 정보가 최신 상태이거나 DB 통신 대상이 없습니다.")
        return

    print(f"🚀 총 {len(hospitals)}개 타겟 병원 정밀 크롤링을 시작합니다!\n")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars"
            ]
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
            viewport={"width": 390, "height": 844},
            java_script_enabled=True
        )
        
        context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = { runtime: {} };
        """)
        
        page = context.new_page()
        page.route("**/*", lambda route: route.abort() 
                   if route.request.resource_type in ["image", "media", "font", "stylesheet"] 
                   else route.continue_())

        for idx, h in enumerate(hospitals, 1):
            h_id = h["id"]
            h_name = h["name"]
            h_addr = h.get("address", "")
            
            print(f"[{idx}/{len(hospitals)}] 🏥 병원명: {h_name} (ID: {h_id[:8]}...)")
            
            query_list = build_search_queries(h_name, h_addr)
            raw_text, raw_html, map_url, search_success = None, None, None, False
            
            for step, query in enumerate(query_list, 1):
                rt, html, url, is_success = crawl_naver_place_with_playwright(query, page)
                if is_success:
                    raw_text, raw_html, map_url = rt, html, url
                    search_success = True
                    break
                else:
                    if step < len(query_list):
                        time.sleep(random.uniform(1.2, 2.2))
            
            if search_success and raw_text and map_url:
                flags = parse_flags(raw_text, raw_html, h_name)
                
                desc_log = flags['description'][:25].replace('\n', ' ') + "..." if flags['description'] else "없음"
                biz_log = "확보 (개행 포함)" if flags['business_hours'] else "없음"
                lunch_log = flags['lunch_time'] if flags['lunch_time'] else "없음"
                parking_log = "가능" if flags['has_parking'] else "없음"
                print(f"      🔹 [정밀 파싱] 진료시간: {biz_log} | 점심: {lunch_log} | 주차: {parking_log} | 소개: {desc_log}")

                if flags['business_hours']:
                    sql = """UPDATE hospitals SET description=?, navermap_url=?, is_silbi=?, has_chuna=?, has_night=?, has_365=?, has_yakchim=?, is_cheopyak=?, has_parking=?, has_ward=?, is_traffic_acc=?, business_hours=?, lunch_time=?, is_hanbang=?, updated_at=CURRENT_TIMESTAMP WHERE id=?"""
                    params = [flags['description'], map_url, flags['is_silbi'], flags['has_chuna'], flags['has_night'], flags['has_365'], flags['has_yakchim'], flags['is_cheopyak'], flags['has_parking'], flags['has_ward'], flags['is_traffic_acc'], flags['business_hours'], flags['lunch_time'], flags['is_hanbang'], h_id]
                else:
                    sql = """UPDATE hospitals SET description=?, navermap_url=?, is_silbi=?, has_chuna=?, has_night=?, has_365=?, has_yakchim=?, is_cheopyak=?, has_parking=?, has_ward=?, is_traffic_acc=?, business_hours=NULL, lunch_time=?, is_hanbang=?, updated_at=CURRENT_TIMESTAMP WHERE id=?"""
                    params = [flags['description'], map_url, flags['is_silbi'], flags['has_chuna'], flags['has_night'], flags['has_365'], flags['has_yakchim'], flags['is_cheopyak'], flags['has_parking'], flags['has_ward'], flags['is_traffic_acc'], flags['lunch_time'], flags['is_hanbang'], h_id]
                    
                execute_d1_query(sql, params)
                print(f"    ✅ [저장 완료] 구분: [{flags['is_hanbang']}] / URL 확보")
            else:
                fallback_type = "한방" if ("한의원" in h_name or "한방병원" in h_name) else "양방"
                execute_d1_query("UPDATE hospitals SET description='N/A', is_hanbang=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", [fallback_type, h_id])
                print(f"    ⚠️ [수집 실패] 'N/A' (기본구분: {fallback_type}) 기록 완료")

            sleep_time = random.uniform(2.5, 4.0)
            print(f"    ⏳ (안티봇 대기: {sleep_time:.1f}초)")
            print("-" * 60)

        browser.close()
    print("\n✨ 고도화 정밀 수집이 완료되었습니다.")

if __name__ == "__main__":
    main()
