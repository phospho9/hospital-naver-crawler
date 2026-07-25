# ---------------------------------------------------------------------------
# 4. 💡 [수정됨] 네이버 플레이스 정밀 크롤링 (광고 회피 및 인코딩 수정)
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
            return None, None
            
        # [수정 1] 검색 결과 전체가 아니라, '플레이스(병원)' 섹션만 추출하여 광고 배제
        soup_search = BeautifulSoup(res.text, "html.parser")
        place_section = soup_search.select_one(".place_section, .sc_new.cs_place, .api_subject_bx")
        
        place_id = None
        if place_section:
            # 병원 정보 고유 ID 타겟팅 (hospital 태그 위주로 탐색)
            place_id_match = re.search(r'(?:hospital/|place/|data-id=")(\d{7,11})', str(place_section))
            if place_id_match:
                place_id = place_id_match.group(1)

        if place_id:
            navermap_url = f"https://m.place.naver.com/hospital/{place_id}/home"
            print(f"    🎯 [ID 확보] 실제 병원 고유번호 추출 성공: {place_id}")
            print(f"    🌐 [URL 확보] 다이렉트 링크: {navermap_url}")
            
            # [수정 2] 한글 깨짐 방지를 위한 UTF-8 강제 인코딩 적용
            res_home = requests.get(f"https://m.place.naver.com/hospital/{place_id}/home", headers=headers, timeout=10)
            res_home.encoding = 'utf-8'  # 한글 깨짐 해결
            soup_home = BeautifulSoup(res_home.text, "html.parser")
            home_text = soup_home.get_text(separator=" ", strip=True)
            
            res_info = requests.get(f"https://m.place.naver.com/hospital/{place_id}/information", headers=headers, timeout=10)
            res_info.encoding = 'utf-8'  # 한글 깨짐 해결
            soup_info = BeautifulSoup(res_info.text, "html.parser")
            info_text = soup_info.get_text(separator=" ", strip=True)
            
            combined_text = (home_text + " " + info_text)[:1500]
            print(f"    📝 [텍스트 수집] 홈+정보 탭 파싱 완료 (총 {len(combined_text)}자)")
            return combined_text, navermap_url
            
        else:
            print("    ⚠️ 병원 고유 ID를 찾을 수 없습니다. 통합검색 기본 텍스트로 대체합니다.")
            return soup_search.get_text(separator=" ", strip=True)[:1000], search_url
            
    except Exception as e:
        print(f"    ❌ 크롤링 중 예외 발생: {e}")
        return None, None
