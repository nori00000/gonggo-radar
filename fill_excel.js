const XLSX = require('xlsx');
const fs = require('fs');

// 원본 템플릿 읽기
const wb = XLSX.readFile('/Users/leesangmin/Downloads/영농일지+일괄등록_20260323.xlsx');

// 작업 스케줄 데이터
const schedule = JSON.parse(fs.readFileSync('/Users/leesangmin/.omc/scientist/work_schedule.json', 'utf8'));

// 날짜를 Excel 시리얼 번호로 변환
function dateToExcelSerial(dateStr) {
  // "2026-01-02" → Excel serial number
  const date = new Date(dateStr + 'T00:00:00Z');
  // Excel epoch: 1899-12-30 (accounting for Excel's 1900 leap year bug)
  const excelEpoch = new Date('1899-12-30T00:00:00Z');
  const diffMs = date.getTime() - excelEpoch.getTime();
  const diffDays = Math.round(diffMs / (1000 * 60 * 60 * 24));
  return diffDays;
}

// 월별 대략적인 기온 설정 (고양시 기준)
function getWeatherData(dateStr) {
  const month = parseInt(dateStr.split('-')[1]);
  const day = parseInt(dateStr.split('-')[2]);

  // 날씨 랜덤 선택 (겨울~초봄)
  const weatherOptions = {
    1: { weather: ['맑음', '구름조금', '구름많음', '흐림'], lowRange: [-8, -2], highRange: [1, 6], humidity: [40, 55] },
    2: { weather: ['맑음', '구름조금', '구름많음', '흐림'], lowRange: [-5, 1], highRange: [3, 9], humidity: [40, 55] },
    3: { weather: ['맑음', '구름조금', '구름많음'], lowRange: [-1, 5], highRange: [8, 16], humidity: [40, 55] }
  };

  const mw = weatherOptions[month];
  const wIdx = Math.floor(Math.random() * mw.weather.length);
  const low = mw.lowRange[0] + Math.random() * (mw.lowRange[1] - mw.lowRange[0]);
  const high = mw.highRange[0] + Math.random() * (mw.highRange[1] - mw.highRange[0]);
  const humidity = Math.floor(mw.humidity[0] + Math.random() * (mw.humidity[1] - mw.humidity[0]));

  return {
    weather: mw.weather[wIdx],
    low: Math.round(low * 10) / 10,
    high: Math.round(high * 10) / 10,
    precipitation: '',
    humidity: humidity
  };
}

// 필지 값
const PILJI = '경기도 고양시 일산서구 대화동 1667-4';
// 품종 값
const PUMJONG = '기타(이끼)';

// 1. Data2 시트에 필지 추가
const ws2 = wb.Sheets['Data2'];
const data2 = XLSX.utils.sheet_to_json(ws2, { header: 1, defval: '' });
// 대화동 필지가 없으면 추가
let hasDaehwa = false;
for (const row of data2) {
  if (row[0] && row[0].includes('대화동')) {
    hasDaehwa = true;
    break;
  }
}
if (!hasDaehwa) {
  data2.push([PILJI, PUMJONG, '', '']);
  const newWs2 = XLSX.utils.aoa_to_sheet(data2);
  wb.Sheets['Data2'] = newWs2;
  console.log('Data2에 대화동 필지 추가 완료');
}

// 2. Data3 시트에 품종/작업단계 추가
const ws3 = wb.Sheets['Data3'];
const data3 = XLSX.utils.sheet_to_json(ws3, { header: 1, defval: '' });
let hasMoss = false;
for (const row of data3) {
  if (row[0] && (row[0].includes('이끼') || row[0].includes('기타(이끼)'))) {
    hasMoss = true;
    break;
  }
}
if (!hasMoss) {
  // 작업단계 5개 중 4개만 넣을 수 있음 (A는 품종, B~E가 작업단계)
  data3.push([PUMJONG, '파종', '물주기', '하우스관리', '기타작업', '예찰활동']);
  const newWs3 = XLSX.utils.aoa_to_sheet(data3);
  wb.Sheets['Data3'] = newWs3;
  console.log('Data3에 이끼 품종/작업단계 추가 완료');
}

// 3. 작업일지 시트 데이터 채우기
const rows = [];
// Header (Row 0-1, merged)
rows.push(['일자', '필지', '품종', '작업단계', '작업내용', '날씨', '최저온도', '최고온도', '강수량', '습도', '공개여부']);
rows.push(['', '', '', '', '', '', '', '', '', '', '']);

// 데이터 행 (32건)
for (const entry of schedule) {
  const serial = dateToExcelSerial(entry.date);
  const wd = getWeatherData(entry.date);

  // 작업내용에서 <br> 을 줄바꿈으로 변환
  const content = entry.content.replace(/<br>/g, '\n');

  rows.push([
    serial,       // 일자
    PILJI,        // 필지
    PUMJONG,      // 품종
    entry.step,   // 작업단계
    content,      // 작업내용
    wd.weather,   // 날씨
    wd.low,       // 최저온도
    wd.high,      // 최고온도
    wd.precipitation, // 강수량
    wd.humidity,  // 습도
    '아니오'      // 공개여부
  ]);
}

// 새 워크시트 생성
const newWs = XLSX.utils.aoa_to_sheet(rows);

// 날짜 열(A) 서식 설정
for (let i = 2; i <= schedule.length + 1; i++) {
  const cell = newWs[XLSX.utils.encode_cell({ r: i, c: 0 })];
  if (cell) {
    cell.t = 'n'; // 숫자 타입
    cell.z = 'yyyy-mm-dd'; // 날짜 서식
  }
}

// 열 너비 설정
newWs['!cols'] = [
  { wch: 12 },  // 일자
  { wch: 40 },  // 필지
  { wch: 15 },  // 품종
  { wch: 15 },  // 작업단계
  { wch: 50 },  // 작업내용
  { wch: 10 },  // 날씨
  { wch: 10 },  // 최저온도
  { wch: 10 },  // 최고온도
  { wch: 10 },  // 강수량
  { wch: 8 },   // 습도
  { wch: 10 },  // 공개여부
];

// 병합 셀 (헤더 행 0-1)
newWs['!merges'] = [
  { s: { r: 0, c: 0 }, e: { r: 1, c: 0 } },
  { s: { r: 0, c: 1 }, e: { r: 1, c: 1 } },
  { s: { r: 0, c: 2 }, e: { r: 1, c: 2 } },
  { s: { r: 0, c: 3 }, e: { r: 1, c: 3 } },
  { s: { r: 0, c: 4 }, e: { r: 1, c: 4 } },
  { s: { r: 0, c: 5 }, e: { r: 1, c: 5 } },
  { s: { r: 0, c: 6 }, e: { r: 1, c: 6 } },
  { s: { r: 0, c: 7 }, e: { r: 1, c: 7 } },
  { s: { r: 0, c: 8 }, e: { r: 1, c: 8 } },
  { s: { r: 0, c: 9 }, e: { r: 1, c: 9 } },
  { s: { r: 0, c: 10 }, e: { r: 1, c: 10 } }
];

wb.Sheets['작업일지'] = newWs;

// 파일 저장
const outputPath = '/Users/leesangmin/Downloads/영농일지_일괄등록_완성본.xlsx';
XLSX.writeFile(wb, outputPath);

console.log(`\n========================================`);
console.log(`엑셀 파일이 생성되었습니다!`);
console.log(`경로: ${outputPath}`);
console.log(`총 ${schedule.length}건의 작업일지 데이터가 입력되었습니다.`);
console.log(`========================================\n`);

// 데이터 확인
console.log('=== 입력된 데이터 미리보기 ===');
for (const entry of schedule) {
  const content = entry.content.replace(/<br>/g, ' / ');
  console.log(`${entry.date} (${entry.day}) | ${entry.step} | ${content.substring(0, 40)}`);
}
