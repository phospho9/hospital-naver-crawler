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
# 2. Cloudflare D1 SQL 실행 함수 (생략 - 기존과 동일)
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
# 3. 검색 키워드 생성 / 4. 정제 / 5. 한방 판별 (생략 - 로직 훌륭함, 기존 동일)
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

def clean_noise_text_with_anchors(raw_text):
    if not raw_text: return ""
    text = raw_text
    review_match = re.search(r'리뷰\s*\d+', text)
    if review_match: text = text[review_match.end():]
        
    simpyung_index = text.find("위 진료정보의 저작권은 건강보험심사평가원")
    if simpyung_index != -1: text = text[:simpyung_index]
    else:
        info_suggest_index = text.find("알고 계신 정보와 다른 정보가 있나요?")
        if info_suggest_index != -1: text = text[:info_suggest_index]
        else:
            loading_index = text.find("로딩중")
            if loading_index != -1: text = text[:loading_index]
                
    text = re.sub(r'Please complete the security verification.*', '', text, flags=re.DOTALL)
    text = re.sub(r'Copyright © NAVER Corp.*', '', text, flags=re.DOTALL)
    text = re.sub(r'\s+', ' ', text).strip()
    return text

def determine_hanbang_type(name, raw_text):
    n = name.lower()
    t = raw_text.lower() if raw_text else ""
    if "한의원" in n or "한방병원" in n: return "한방"
    cooperation_keywords = r'(한양방|한·양방|양한방|한양방협진|한·양방협진|양·한방협진|협진병원|협진진료|의사·한의사|한의사·의사)'
    if re.search(cooperation_keywords, t) or re.search(cooperation_keywords, n): return "한양방"
    return "양방"

# ---------------------------------------------------------------------------
# 6. Playwright 크롤링 (💡 속도 최적화 적용 구역)
# ---------------------------------------------------------------------------
def crawl_naver_place_with_playwright(query, page):
    search_url = f"https://m.search.naver.com/search.naver?query={query}"
    print(f"    📍 [검색] {query}")
    
    try:
        # domcontentloaded로 설정하여 불필요한 네트워크 대기 방지
        page.goto(search_url, timeout=10000, wait_until="domcontentloaded")
        page.wait_for_timeout(500) # 기존 2000ms -> 500ms 단축
        
        place_id = None
        html_content = page.content()
        
        place_id_match = re.search(r'(?:hospital/|place/|data-id=")(\d{7,11})', html_content)
        if place_id_match:
            place_id = place_id_match.group(1)

        if place_id:
            navermap_url = f"https://m.place.naver.com/hospital/{place_id}/home"
            
            page.goto(navermap_url, timeout=10000, wait_until="domcontentloaded")
            page.wait_for_timeout(800) # 렌더링 최소 대기 (기존 2000ms -> 800ms)
            
            try:
                click_selectors = ["text=펼쳐보기", "text=영업시간", ".g2u4Z", ".group_fold", "[aria-expanded='false']"]
                for selector in click_selectors:
                    try:
                        elements = page.locator(selector)
                        for i in range(min(elements.count(), 2)):
                            elements.nth(i).click(timeout=500)
                            page.wait_for_timeout(100) # 클릭 후 렌더링 0.1초 대기
                    except:
                        pass
            except Exception:
                pass
                
            home_text = page.inner_text("body")
            raw_html = page.content()
            
            try:
                page.goto(f"https://m.place.naver.com/hospital/{place_id}/information", timeout=10000, wait_until="domcontentloaded")
                page.wait_for_timeout(500) # 기존 1500ms -> 500ms 단축
                info_text = page.inner_text("body")
            except Exception:
                info_text = ""
                
            raw_text = home_text + "\n" + info_text
            return raw_text, raw_html, navermap_url, True
        else:
            return None, html_content, search_url, False
            
    except Exception as e:
        print(f"    ❌ 크롤링 에러: {e}")
        return None, None, None, False

# ---------------------------------------------------------------------------
# 7. 진료시간/플래그 파서 (생략 - 기존 동일)
# ---------------------------------------------------------------------------
def parse_flags(text, raw_html, name=""):
    flags = {
        'is_silbi': 0, 'has_chuna': 0, 'has_night': 0, 'has_365': 0, 
        'has_yakchim': 0, 'is_cheopyak': 0, 'has_parking': 0, 
        'has_ward': 0, 'is_traffic_acc': 0, 'business_hours': None, 'lunch_time': None,
        'is_hanbang': '양방'
    }
    if not text or not raw_html: return flags
    t = text.lower()
    raw_html_lower = raw_html.lower()
    n = name.lower()
    
    flags['is_hanbang'] = determine_hanbang_type(name, t + " " + raw_html_lower)

    weekdays = ['월요일', '화요일', '수요일', '목요일', '금요일', '토요일', '일요일']
    schedule = {day: '정보 없음' for day in weekdays}
    schedule['공휴일'] = ''
    schedule['점심시간'] = ''
    
    time_pattern = re.compile(
        r'(월요일|화요일|수요일|목요일|금요일|토요일|일요일|평일|공휴일|점심시간|휴게시간)\s*[:]?\s*'
        r'(\d{1,2}:\d{2}\s*[~–\-]\s*\d{1,2}:\d{2}|휴무|휴진|정기휴무)'
    )
    matches = time_pattern.findall(text)
    
    for match in matches:
        key = match[0].replace('매주 ', '').strip()
        val = match[1].replace('–', '~').replace('-', '~')
        
        if key == '평일':
            for d in ['월요일', '화요일', '수요일', '목요일', '금요일']:
                if schedule[d] == '정보 없음': schedule[d] = val
        elif key in weekdays: schedule[key] = val
        elif '공휴일' in key: schedule['공휴일'] = val
        elif '점심' in key or '휴게' in key: schedule['점심시간'] = val

    formatted_hours = []
    for day in weekdays:
        if schedule[day] != '정보 없음': formatted_hours.append(f"{day}: {schedule[day]}")
    if schedule['공휴일']: formatted_hours.append(f"공휴일: {schedule['공휴일']}")
    if formatted_hours: flags['business_hours'] = "\n".join(formatted_hours)
        
    if schedule['점심시간']: flags['lunch_time'] = schedule['점심시간']
    else:
        lunch_match = re.search(r'(휴게시간|점심시간|브레이크)[^\d]*(\d{1,2}:\d{2})\s*[~–\-]\s*(\d{1,2}:\d{2})', text)
        if lunch_match: flags['lunch_time'] = f"{lunch_match.group(2)} ~ {lunch_match.group(3)}"

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
        flags.update({'has_chuna':1, 'is_cheopyak':1, 'has_yakchim':1, 'has_night':1, 'has_365':0, 'has_parking':1, 'has_ward':0, 'is_traffic_acc':1, 'is_hanbang':'한방'})
    return flags

# ---------------------------------------------------------------------------
# 8. 메인 파이프라인 실행
# ---------------------------------------------------------------------------
def main():
    # 💡 [최적화 3] LIMIT 100 -> 50 축소 (1회 실행을 짧게 끊어서 안전성 확보)
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
        print("🎉 모든 병원 정보가 최신 상태이거나 DB 통신에 실패했습니다.")
        return

    print(f"🚀 총 {len(hospitals)}개의 타겟 병원을 찾았습니다. Playwright 정밀 크롤링을 시작합니다!\n")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
            viewport={"width": 390, "height": 844}
        )
        
        page = context.new_page()
        
        # 💡 [최적화 1] 이미지, CSS, 폰트, 미디어 등 불필요한 트래픽 완벽 차단 (텍스트만 긁어옴)
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
                        time.sleep(0.5) # 실패 시 재시도 대기시간 축소
            
            if search_success and raw_text and map_url:
                cleaned_text = clean_noise_text_with_anchors(raw_text)
                flags = parse_flags(cleaned_text, raw_html, h_name)
                
                # 진료시간 유무에 따른 업데이트 쿼리 분기
                if flags['business_hours']:
                    sql = """UPDATE hospitals SET description=?, navermap_url=?, is_silbi=?, has_chuna=?, has_night=?, has_365=?, has_yakchim=?, is_cheopyak=?, has_parking=?, has_ward=?, is_traffic_acc=?, business_hours=?, lunch_time=?, is_hanbang=?, updated_at=CURRENT_TIMESTAMP WHERE id=?"""
                    params = [cleaned_text, map_url, flags['is_silbi'], flags['has_chuna'], flags['has_night'], flags['has_365'], flags['has_yakchim'], flags['is_cheopyak'], flags['has_parking'], flags['has_ward'], flags['is_traffic_acc'], flags['business_hours'], flags['lunch_time'], flags['is_hanbang'], h_id]
                else:
                    sql = """UPDATE hospitals SET description=?, navermap_url=?, is_silbi=?, has_chuna=?, has_night=?, has_365=?, has_yakchim=?, is_cheopyak=?, has_parking=?, has_ward=?, is_traffic_acc=?, is_hanbang=?, updated_at=CURRENT_TIMESTAMP WHERE id=?"""
                    params = [cleaned_text, map_url, flags['is_silbi'], flags['has_chuna'], flags['has_night'], flags['has_365'], flags['has_yakchim'], flags['is_cheopyak'], flags['has_parking'], flags['has_ward'], flags['is_traffic_acc'], flags['is_hanbang'], h_id]
                    
                execute_d1_query(sql, params)
                print(f"    ✅ [저장 성공] 한/양방: [{flags['is_hanbang']}] / URL 확보 완료")
            else:
                fallback_type = "한방" if ("한의원" in h_name or "한방병원" in h_name) else "양방"
                execute_d1_query("UPDATE hospitals SET description='N/A', is_hanbang=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", [fallback_type, h_id])
                print(f"    ⚠️ [수집 실패] 'N/A' (기본구분: {fallback_type}) 처리 완료")

            # 💡 [최적화 2] 1건 처리 후 멍때리는 시간 대폭 단축 (2.5~4.0초 -> 0.8~1.5초)
            time.sleep(random.uniform(0.8, 1.5))
            print("-" * 50)

        browser.close()
    print("\n✨ 정밀 분류 및 수집이 초고속으로 완료되었습니다.")

if __name__ == "__main__":
    main()
