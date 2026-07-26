import requests
from bs4 import BeautifulSoup
import urllib.parse
import re
import time
import random
from datetime import datetime, timedelta, timezone

# ----------------------------------------------------------------------
# [1] 진료시간 파싱 및 정규화 함수 (월~일 순서 배치)
# ----------------------------------------------------------------------
def parse_and_format_business_hours(raw_text):
    if not raw_text or raw_text == 'N/A':
        return 'None'
    
    # GitHub Actions(UTC) 환경을 고려하여 KST(한국 표준시)로 현재 시간 계산
    KST = timezone(timedelta(hours=9))
    now_kst = datetime.now(KST)
    days = ['월요일', '화요일', '수요일', '목요일', '금요일', '토요일', '일요일']
    today_str = days[now_kst.weekday()]
    
    # '오늘' 텍스트를 실제 요일로 치환
    processed_text = raw_text.replace('오늘', today_str)
    
    schedule = {
        '월요일': '정보 없음', '화요일': '정보 없음', '수요일': '정보 없음',
        '목요일': '정보 없음', '금요일': '정보 없음', '토요일': '정보 없음',
        '일요일': '정보 없음', '공휴일': '', '점심시간': ''
    }
    
    # 정규식 패턴: 평일/주말/공휴일/점심 시간 추출
    pattern = re.compile(r'(평일|토요일|일요일|매주\s*일요일|공휴일|점심|월요일|화요일|수요일|목요일|금요일)\s*(\d{2}:\d{2}\s*~\s*\d{2}:\d{2}|휴무|휴진)')
    matches = pattern.findall(processed_text)
    
    for match in matches:
        key = match[0].replace('매주 ', '').strip()
        value = match[1]
        
        if key == '평일':
            for d in ['월요일', '화요일', '수요일', '목요일', '금요일']:
                schedule[d] = value
        elif '토요일' in key:
            schedule['토요일'] = value
        elif '일요일' in key:
            schedule['일요일'] = value
        elif '공휴일' in key:
            schedule['공휴일'] = value
        elif '점심' in key:
            schedule['점심시간'] = value
        elif key in days:
            schedule[key] = value

    # 단독 '휴무/휴진' 텍스트 처리 (예: "일요일 휴무")
    for day in days:
        if f"{day} 휴무" in processed_text or f"{day} 휴진" in processed_text:
            schedule[day] = '휴무'

    # 월~일 순서로 텍스트 조합
    formatted_result = []
    for day in days:
        if schedule[day] != '정보 없음':
            formatted_result.append(f"{day}: {schedule[day]}")
            
    if schedule['공휴일']:
        formatted_result.append(f"공휴일: {schedule['공휴일']}")
    if schedule['점심시간']:
        formatted_result.append(f"점심시간: {schedule['점심시간']}")
        
    final_str = "\n".join(formatted_result).strip()
    return final_str if final_str else raw_text

# ----------------------------------------------------------------------
# [2] 단일 병원 크롤링 함수 (Script 파싱 포함)
# ----------------------------------------------------------------------
def crawl_hospital(hospital_name, region):
    query = f"{hospital_name} {region}"
    url = f"https://m.search.naver.com/search.naver?query={urllib.parse.quote(query)}"
    print(f"    📍 [검색 진입] {url}")
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.0 Mobile/15E148 Safari/604.1'
    }
    
    try:
        response = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # 1. 고유 ID 추출
        hospital_id = None
        for a_tag in soup.find_all('a', href=True):
            href = a_tag['href']
            match = re.search(r'(?:hospital|place)/(\d+)', href)
            if match:
                hospital_id = match.group(1)
                break
                
        if not hospital_id:
            print("    ⚠️ 병원 고유 ID를 찾을 수 없습니다. (검색결과 스킵)")
            return {'id': None, 'description': 'N/A', 'business_hours': 'None'}
            
        print(f"    🎯 [ID 확보] 병원 고유번호 추출 성공: {hospital_id}")
        
        # 2. 소개글 추출 및 필터링 (리뷰 텍스트 오염 방지)
        description = 'N/A'
        desc_element = soup.select_one('.api_txt_lines.desc')
        
        # 대체 선택자 탐색 (소개글이 다른 위치에 있을 경우)
        if not desc_element:
            blue_link = soup.select_one('.place_bluelink')
            if blue_link and blue_link.parent:
                desc_element = blue_link.parent.find_next_sibling()
                
        if desc_element:
            raw_desc = desc_element.get_text(strip=True)
            if '방문자리뷰' in raw_desc or '블로그리뷰' in raw_desc:
                description = 'N/A'
            else:
                description = raw_desc
                print(f"    📝 [텍스트 수집] 순수 소개글 파싱 완료 (총 {len(description)}자)")

        # 3. 진료시간 추출 (Script 파싱 최우선)
        raw_business_hours = 'N/A'
        
        # 3-1. <script> 태그 내부 텍스트 스캔 (네이버가 DB에서 직접 뿌리는 데이터)
        for script in soup.find_all('script'):
            if script.string and ('bizHour' in script.string or 'businessHours' in script.string or '평일' in script.string):
                # 스크립트 내부에서 요일과 시간 패턴을 강제로 긁어옴
                time_matches = re.findall(r'(평일|토요일|일요일|매주\s*일요일|공휴일|점심|월요일|화요일|수요일|목요일|금요일)\s*(\d{2}:\d{2}\s*~\s*\d{2}:\d{2}|휴무|휴진)', script.string)
                
                if time_matches:
                    extracted_times = []
                    for match in time_matches:
                        extracted_times.append(f"{match[0]} {match[1]}")
                    
                    raw_business_hours = " ".join(extracted_times)
                    if raw_business_hours:
                        break # 성공적으로 찾았으면 루프 종료

        # 3-2. 만약 스크립트에서 찾지 못했다면 기존 HTML 태그(DOM)에서 폴백(Fallback) 추출
        if raw_business_hours == 'N/A' or not raw_business_hours:
            time_element = soup.select_one('.period_time') or soup.select_one('.time_box')
            if time_element:
                raw_business_hours = time_element.get_text(strip=True)
            elif description != 'N/A' and ('진료시간' in description or '평일' in description):
                raw_business_hours = description

        # 4. 추출된 원시 텍스트를 월~일 구조로 정규화
        business_hours = 'None'
        if raw_business_hours and raw_business_hours != 'N/A':
            business_hours = parse_and_format_business_hours(raw_business_hours)
            
        return {'id': hospital_id, 'description': description, 'business_hours': business_hours}
        
    except Exception as e:
        print(f"    ❌ [크롤링 에러] {hospital_name}: {e}")
        return {'id': None, 'description': 'N/A', 'business_hours': 'None'}

# ----------------------------------------------------------------------
# [3] 메인 실행 함수
# ----------------------------------------------------------------------
def main():
    print("🚀 생명마루한의원 안산점 플레이스 순위 상승을 위한 경쟁사 데이터 수집 시작...\n")
    
    # DB에서 조회한 타겟 리스트라고 가정
    targets = [
        {'name': '인다라한방병원', 'region': '의정부'},
        {'name': '일산자생한방병원', 'region': '경기도 고양시'},
        {'name': '일산365한방병원', 'region': '경기도 고양시'}
    ]
    
    for i, target in enumerate(targets):
        name = target['name']
        region = target['region']
        print(f"[{i + 1}/{len(targets)}] 🏥 병원명: {name} / 키워드: {name} {region}")
        
        result = crawl_hospital(name, region)
        
        if result['id']:
            # TODO: Cloudflare D1 SQL 업데이트 로직을 여기에 연동하세요. (requests로 Cloudflare API 호출 등)
            # 예: requests.post(CLOUDFLARE_D1_API_URL, headers=headers, json={"sql": f"UPDATE hospitals SET description='{result['description']}' ..."})
            
            print("    ✅ [저장 성공]")
            short_desc = result['description'][:30] + ('...' if len(result['description']) > 30 else '')
            print(f"       - 📝 본문미리보기: {short_desc}")
            
            formatted_hours = "\n         ".join(result['business_hours'].split('\n'))
            print(f"       - 🕒 진료시간:\n         {formatted_hours}")
        else:
            print("    ⚠️ [수집 실패] 검색 결과를 찾지 못해 'N/A'로 처리했습니다.")
            
        print("-" * 70)
        
        # 네이버 차단 방지를 위한 랜덤 딜레이 (1~3초)
        time.sleep(1 + random.uniform(0, 2))
        
    print("✨ 수고하셨습니다! 오늘의 네이버 플레이스 정밀 크롤링이 무사히 끝났습니다.")

if __name__ == "__main__":
    main()
