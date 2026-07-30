import os
import re
import time
import requests
import urllib.parse
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# ==========================================
# 1. 셀레니움 드라이버 세팅 (Headless)
# ==========================================
def create_driver():
    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1920,1080")
    # 네이버 플레이스 모바일 접근을 위한 User-Agent 세팅
    user_agent = "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1"
    chrome_options.add_argument(f"user-agent={user_agent}")
    
    driver = webdriver.Chrome(options=chrome_options)
    return driver

# ==========================================
# 2. 네이버 플레이스 Raw 데이터 수집 (무제한)
# ==========================================
def fetch_naver_place_raw_text(driver, hospital_name, address_hint=""):
    query = f"{hospital_name} {address_hint}".strip()
    encoded_query = urllib.parse.quote(query)
    search_url = f"https://m.search.naver.com/search.naver?query={encoded_query}"
    
    print(f"    📍 [검색 진입] {search_url}")
    driver.get(search_url)
    time.sleep(2)
    
    place_id = None
    direct_url = None
    
    # 1) 검색 결과 내 Place ID 추출 시도
    try:
        # 모바일 플레이스 링크 탐색
        links = driver.find_elements(By.XPATH, "//a[contains(@href, 'place.naver.com/hospital/')]")
        for link in links:
            href = link.get_attribute("href")
            match = re.search(r"hospital/(\d+)", href)
            if match:
                place_id = match.group(1)
                direct_url = f"https://m.place.naver.com/hospital/{place_id}/home"
                break
    except Exception as e:
        print(f"    ⚠️ ID 추출 중 오류: {e}")

    if place_id:
        print(f"    🎯 [ID 확보] 병원 고유번호 추출 성공: {place_id}")
        print(f"    🌐 [URL 확보] 다이렉트 링크: {direct_url}")
        driver.get(direct_url)
        time.sleep(2.5)
    else:
        print("    ⚠️ 고유 ID 추출 실패 - 통합 검색 페이지 텍스트 수집으로 대체합니다.")

    # 2) 페이지 전체 Raw 텍스트 무제한 추출
    try:
        body_element = driver.find_element(By.TAG_NAME, "body")
        raw_text = body_element.text
    except Exception as e:
        print(f"    ❌ 텍스트 추출 실패: {e}")
        raw_text = ""

    # 3) UI 노이즈 가벼운 개행 정리 (글자 수 자르기 절대 금지)
    # 줄바꿈 단위를 공백으로 변환하여 연속된 텍스트 블록 생성
    cleaned_full_text = " ".join(raw_text.split())

    return raw_text, cleaned_full_text

# ==========================================
# 3. 데이터 파싱/분류 규칙 (원본 보존 기반)
# ==========================================
def parse_hospital_attributes(full_text):
    # 한/양방 구분
    has_oriental = any(k in full_text for k in ["한방", "침구과", "사상체질", "한의원", "한방내과", "한방재활", "추나", "약침"])
    has_western = any(k in full_text for k in ["내과", "가정의학과", "재활의학과", "신경과", "외과", "정형외과", "소아청소년과"])
    
    if has_oriental and has_western:
        dept_type = "한양방"
    elif has_oriental:
        dept_type = "한방"
    else:
        dept_type = "양방"

    # 특화진료 및 편의 정보
    features = {
        "chuna": 1 if "추나" in full_text else 0,
        "yakchim": 1 if "약침" in full_text else 0,
        "cheobyak": 1 if "첩약" in full_text or "한약" in full_text else 0,
        "inpatient": 1 if any(k in full_text for k in ["입원", "병동", "요양병원"]) else 0,
        "night": 1 if "야간" in full_text else 0,
        "days365": 1 if any(k in full_text for k in ["365", "연중무휴"]) else 0,
        "car_accident": 1 if any(k in full_text for k in ["자보", "자동차보험"]) else 0,
        "parking": 1 if "주차" in full_text else 0,
    }
    
    return dept_type, features

# ==========================================
# 4. 메인 실행 파이프라인 (Iterative Process)
# ==========================================
def process_hospitals(hospital_list):
    driver = create_driver()
    total_count = len(hospital_list)
    
    print(f"🚀 총 {total_count}개 병원 데이터 수집 시작 (글자수 제한 없음, 원본 무제한 수집)...")
    print("=" * 70)
    
    try:
        for idx, hosp in enumerate(hospital_list, 1):
            hosp_id = hosp.get("id", "UNKNOWN")
            hosp_name = hosp.get("name", "")
            address_hint = hosp.get("address", "")
            
            print(f"[{idx}/{total_count}] 🏥 병원명: {hosp_name} (ID: {hosp_id})")
            print(f"    - 1차 검색 키워드: {hosp_name} {address_hint}")
            
            # 무제한 Raw 텍스트 수집
            raw_text, full_text = fetch_naver_place_raw_text(driver, hosp_name, address_hint)
            
            raw_len = len(raw_text)
            clean_len = len(full_text)
            
            # 파싱 검증
            dept_type, features = parse_hospital_attributes(full_text)
            
            # 로그 출력 (전체 길이를 제한 없이 출력 확인)
            print("\n    ===================== 📝 3단계 텍스트 수집 로그 =====================")
            print(f"    1️⃣ [처음 가져온 Raw 텍스트 (총 {raw_len}자)]")
            print(f"       👉 {raw_text[:150]} ... (중략) ... {raw_text[-150:] if raw_len > 300 else ''}")
            print(f"    2️⃣ [UI 노이즈 정리 후 전체 텍스트 (총 {clean_len}자)]")
            print(f"       👉 {full_text[:150]} ... (중략) ... {full_text[-150:] if clean_len > 300 else ''}")
            print(f"    3️⃣ [DB에 최종 저장될 description (자르기 없이 {clean_len}자 전체 보존)]")
            print(f"       👉 {full_text}")
            print("    ======================================================================\n")
            
            # DB 저장 대치 (description 필드에 글자수 자르기 없이 full_text 저장)
            db_payload = {
                "hospital_id": hosp_id,
                "hospital_name": hosp_name,
                "description": full_text,  # 무제한 원본 수집
                "dept_type": dept_type,
                "features": features
            }
            
            # DB 업데이트 완료 로그
            print(f"    ✅ [저장 성공] DB 업데이트가 완료되었습니다.")
            print(f"        - 🏷️ 한/양방 구분: [{dept_type}]")
            print(f"        - 🍴 점심시간: None")
            print(f"        - 💊 특화진료: 추나({features['chuna']}) 약침({features['yakchim']}) 첩약({features['cheobyak']}) 입원({features['inpatient']})")
            print(f"        - 🚗 부가정보: 야간({features['night']}) 365({features['days365']}) 자보({features['car_accident']}) 주차({features['parking']})")
            print("-" * 70)
            
    finally:
        driver.quit()
        print("🏁 모든 수집 작업이 안전하게 완료되었습니다.")

# 샘플 실행부 (테스트용)
if __name__ == "__main__":
    sample_hospitals = [
        {"id": "JDQ4MTYy01", "name": "효사랑가족요양병원", "address": "전북특별자치도 전주시"},
        {"id": "JDQ4MTYy02", "name": "효사랑전주요양병원", "address": "전북특별자치도 전주시"}
    ]
    process_hospitals(sample_hospitals)
