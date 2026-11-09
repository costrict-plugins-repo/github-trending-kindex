"""Offline authored fixture construction; never imports or executes Kindex."""
import hashlib
import json
from pathlib import Path

HERE = Path(__file__).parent
notes = [
('s01', 'Pottery studio kiln shutdown', 'On 12 February 2026, Mira found that the studio kiln stopped near 800 degrees because the lid interlock connector had worked loose. Reseating the blue connector cleared the fault. The heating elements tested normally.', 'studio'),
('s02', 'Ridge observatory tracking', 'During the March open night, Tomas fixed the observatory telescope drifting east by switching the controller from southern to northern hemisphere mode. The alignment stars and tripod level were already correct.', 'observatory'),
('s03', 'Clinic specimen freezer alarm', 'The Birch clinic freezer alarm sounded each afternoon after deliveries. Hana found cartons blocking the rear air return. Moving the cartons restored temperature without replacing the thermostat.', 'clinic'),
('s04', 'School trip transport', 'For the Cedar school wetland trip on 18 May, Leena booked the smaller bus because the bridge by the reserve has a six-ton weight limit. Students meet at the east gate at 08:20.', 'school'),
('s05', 'Workshop bicycle vibration', 'Oren traced the cargo bicycle shudder under braking to a contaminated front rotor. He cleaned the rotor and replaced the pads; tightening the headset had not helped.', 'workshop'),
('s06', 'Cooperative payroll pennies', 'The Harbour cooperative payroll totals were off by a few cents because hours were rounded on each daily entry. Jules changed the export to round only the final weekly total per person.', 'cooperative'),
('s07', 'Choir spring rehearsal venue', 'Nadia moved the spring choir rehearsal to Lantern Hall because the usual room was being repainted. The hall caretaker is Pavel and the access key is collected from the bakery next door.', 'choir'),
('s08', 'Greenhouse wilt investigation', 'The north greenhouse tomato beds wilted despite wet soil. Dev found a blocked return drain causing waterlogged roots. Clearing the return restored drainage; adding more irrigation had made the problem worse.', 'garden'),
('s09', 'Museum display fading', 'Inez chose pigment printing for the maritime museum labels after the trial dye labels faded beside the south window. The wording and font size remained unchanged.', 'museum'),
('s10', 'Island ferry luggage plan', 'For the 6 June island survey, Amara reserved the first ferry and put sample cases on the wheeled cart. The passenger gangway has steps, so the cart must use the vehicle ramp with staff assistance.', 'survey'),
('s11', 'Community pantry collection', 'The Elm pantry now collects bread after 16:30 on Thursdays. Bakery manager Rowan requested the later pickup so unsold loaves are counted before donation.', 'pantry'),
('s12', 'Blue glaze trial estimate', 'At the first pottery planning meeting, Mira estimated that cobalt glaze batch B17 should be fired at cone 6. This estimate was recorded before the supplier certificate arrived.', 'studio'),
('s13', 'Blue glaze supplier correction', 'Correction to the earlier B17 estimate: the supplier certificate specifies cone 5, not cone 6. Mira confirmed that batch B17 should use cone 5. This replaces the first planning estimate.', 'studio'),
('s14', 'Wetland grant attachment', 'Rafi discovered the wetland restoration grant upload was rejected because the habitat map PDF exceeded the twelve-megabyte limit. Exporting the map at 150 dpi reduced it enough for submission.', 'conservation'),
('s15', 'Archive scanner streaks', 'At the town archive, Celia eliminated a vertical streak on scanned letters by cleaning the narrow feeder glass. The broad flatbed glass was clean and did not cause the streak.', 'archive'),
('s16', 'Workshop heat pump noise', 'The workshop heat pump rattled only during defrost. Beck found its condensate pipe tapping the metal wall bracket. A rubber spacer stopped the tapping without changing compressor settings.', 'workshop'),
('s17', 'Library tablet sign-in', 'Library loan tablets repeatedly returned to the sign-in screen after waking. Edda found the device clock was seventeen minutes slow. Synchronizing time stopped the session-token rejection.', 'library'),
('s18', 'River sampling bottles', 'Niko marked the upstream river samples with green caps and downstream samples with white caps for the April survey. Both sets used identical amber bottles and the same preservative.', 'survey'),
('s19', 'Inn late arrival instructions', 'For the orchard survey overnight stay, Suri arranged late entry to Juniper Inn through the courtyard door. The main lobby locks at 21:00; the courtyard bell reaches the night caretaker.', 'survey'),
('s20', 'Seed swap draft room', 'The first seed swap planning note reserved the Maple room for 22 March at 10:00. That booking was provisional pending the building inspection.', 'garden'),
('s21', 'Seed swap confirmed room', 'The confirmed seed swap on 22 March at 10:00 is in the Willow room. The Maple room failed its building inspection, so the provisional Maple booking is superseded.', 'garden'),
('s22', 'Volunteer form duplicate submissions', 'The volunteer signup form created two records when someone double-clicked Submit. Priya added a request idempotency key so retrying the same submission reuses the first record. A longer visual animation was rejected.', 'volunteers'),
('s23', 'Photo backup verification', 'After the field photography trip, Luka verified the backup by comparing SHA-256 checksums for every copied file. File counts alone had missed an earlier truncated image.', 'photography'),
('s24', 'Community lunch sauce', 'For the community lunch, Farah kept the sesame dressing in a separate labeled jug because two guests reported sesame allergy. The plain lentil dish contains no sesame.', 'kitchen'),
('s25', 'Pottery studio kiln shutdown', 'On 12 February 2026, Mira found that the studio kiln stopped near 800 degrees because the lid interlock connector had worked loose. Reseating the blue connector cleared the fault. The heating elements tested normally.', 'studio'),
('s26', 'Field antenna packing', 'For project Violet, the field antenna is packed in the orange tube. Morgan moved it from the grey case after the hinge cracked. The tube belongs to the radio survey kit.', 'violet'),
('s27', 'Field antenna packing', 'For project Amber, the field antenna is packed in the green crate. Morgan keeps the battery in the orange tube. These containers belong to the weather survey kit.', 'amber'),
('s28', 'Telescope loan collection', 'The school telescope loan is collected from the south entrance on Friday. Tomas asked the borrower to bring the padded bag. No tracking repair was discussed.', 'school'),
('s29', 'Pharmacy door alarm', 'At the Birch pharmacy, Hana adjusted the door alarm delay to allow delivery staff to wheel cartons inside. The medicine refrigerator passed its temperature inspection.', 'pharmacy'),
('s30', 'Museum loading entrance', 'The maritime museum delivery labels must show the north loading entrance. Inez confirmed that courier vans cannot stop at the south visitor entrance.', 'museum'),
('s31', 'Pantry freezer purchase', 'Rowan donated a chest freezer to the Elm pantry for frozen meals. The electrician will install a dedicated outlet before it is switched on.', 'pantry'),
('s32', 'Private music practice', 'Nadia keeps her personal violin mute in the blue desk drawer at home. This private note is unrelated to the choir venue or shared hall access.', 'personal'),
]
sources = []
for sid, title, content, scope in notes:
    sources.append({'id':sid, 'title':title, 'content':content, 'scope':scope,
                    'audience':'private' if sid == 's32' else 'team',
                    'type':'concept', 'duplicate_of':'s01' if sid == 's25' else None,
                    'supersedes': {'s13':'s12', 's21':'s20'}.get(sid)})
# Queries are authored from the source facts, without generated cues or system output.
# A hint is additional remembered context available at inquiry time, not an answer.
queries = [
('q01','What fixed the pottery oven that kept cutting out partway through getting hot?','Mira looked at it in February.',['s01','s25'],'studio','symptom'),
('q02','Why did the stars keep sliding out of view at the open evening?','Tomas was running the ridge observatory.',['s02'],'observatory','paraphrase'),
('q03','What was behind the daily afternoon cold-storage warning?','Hana checked it after the clinic deliveries.',['s03'],'clinic','symptom'),
('q04','Why did we choose the less roomy vehicle for the children visiting the marsh?','Leena arranged the Cedar outing in May.',['s04'],'school','paraphrase'),
('q05','What actually stopped the front end shaking when the delivery bike slowed down?','Oren had already tried tightening the headset.',['s05'],'workshop','symptom'),
('q06','How did we get rid of the tiny differences in the staff payment export?','Jules was working on the Harbour weekly totals.',['s06'],'cooperative','paraphrase'),
('q07','Where do I pick up access for the replacement singing practice room?','Nadia moved the spring choir rehearsal.',['s07'],'choir','context'),
('q08','Why were the plants drooping even though their beds were soggy?','Dev investigated the north greenhouse.',['s08'],'garden','symptom'),
('q09','Which printing method did we settle on for the display cards near daylight?','Inez was testing labels for the maritime museum.',['s09'],'museum','context'),
('q10','How were we supposed to get the wheeled sampling kit aboard the boat?','Amara planned the June island trip.',['s10'],'survey','paraphrase'),
('q11','When should I fetch the donated loaves, and why that time?','Rowan manages the bakery serving Elm pantry.',['s11'],'pantry','person'),
('q12','What is the corrected firing setting for that blue glaze batch?','Mira received the supplier certificate for B17.',['s13'],'studio','correction'),
('q13','How did we get the habitat diagram accepted by the funding portal?','Rafi handled the wetland restoration application.',['s14'],'conservation','context'),
('q14','Which surface needed cleaning to stop the line through digitized correspondence?','Celia was using the archive document feeder.',['s15'],'archive','symptom'),
('q15','What cured that banging sound whenever the heating unit thawed itself?','Beck checked the workshop heat pump.',['s16'],'workshop','symptom'),
('q16','Why did the borrowed devices keep asking patrons to log in again after sleep?','Edda found a timing problem on the library tablets.',['s17'],'library','symptom'),
('q17','How can I tell the water collected before the village from the water collected after it?','Niko assigned cap colors during the April river survey.',['s18'],'survey','paraphrase'),
('q18','How do we get into our lodging after reception closes?','Suri arranged Juniper for the orchard fieldwork.',['s19'],'survey','context'),
('q19','Which room is the seed exchange actually using after the inspection?','The event is at ten on 22 March.',['s21'],'garden','correction'),
('q20','What prevents a repeated click from registering the same helper twice?','Priya fixed the volunteer signup form.',['s22'],'volunteers','symptom'),
('q21','How do we know the copied photographs are intact rather than merely present?','Luka checked the files after the field trip.',['s23'],'photography','paraphrase'),
('q22','Why was the lunch sauce served separately, and could everyone eat the plain dish?','Farah organized the community lentil lunch.',['s24'],'kitchen','context'),
('q23','Where did Morgan put the antenna after its old container broke?','I am packing the Violet radio survey kit.',['s26'],'violet','scope'),
('q24','Where is the antenna for my field kit?','I am working on project Amber with Morgan.',['s27'],'amber','scope'),
('q25','Where should the driver take the museum shipment?','Inez clarified the entrance for courier vans.',['s30'],'museum','distractor'),
('q26','What has to happen before the donated frozen-food storage can be used?','Rowan gave Elm pantry the chest freezer.',['s31'],'pantry','distractor'),
('q27','What was the original proposed room for the seed swap before the building inspection?','The first planning note was for 22 March.',['s20'],'garden','historical'),
('q28','What firing setting had Mira first estimated before the supplier corrected it?','This is the older B17 planning note.',['s12'],'studio','historical'),
('q29','What is the numeric keypad code for getting into the rehearsal hall?','Nadia arranged the spring choir room.',[],'choir','no_answer'),
('q30','Who manufactures the freezer at the clinic?','Hana investigated its afternoon alarm.',[],'clinic','no_answer'),
('q31','What did the ferry tickets cost per person?','Amara booked the first sailing for the June island survey.',[],'survey','no_answer'),
('q32','What exact date will the pantry electrician finish installing the outlet?','Rowan donated the chest freezer.',[],'pantry','no_answer'),
('q33','Where does Nadia keep her personal violin accessory?','I only have access to the shared choir notes.',[],'choir','scope_no_answer'),
('q34','What time and entrance were chosen for assembling the school outing?','Leena booked transport to the wetland reserve.',['s04'],'school','time'),
('q35','Why did Hana change the delivery entrance warning delay?','This was the Birch pharmacy visit.',['s29'],'pharmacy','distractor'),
('q36','Where and when should the borrower collect the telescope?','Tomas arranged the school loan.',['s28'],'school','distractor'),
]
items=[]
for qid, inquiry, hint, expected, scope, category in queries:
    for condition in ('without_hints','with_hints'):
        items.append({'id':f'{qid}_{condition}', 'pair_id':qid, 'condition':condition,
                      'query':inquiry, 'hint':hint if condition == 'with_hints' else '',
                      'scope':scope, 'allowed_audiences':['team','public'],
                      'expected_relevant_source_ids':expected, 'category':category,
                      'answerable':bool(expected)})
def save(name,obj):
    data=(json.dumps(obj,ensure_ascii=False,indent=2)+'\n').encode()
    (HERE/name).write_bytes(data)
    return hashlib.sha256(data).hexdigest()
a=save('sources.json', {'fixture_version':1, 'fictional':True, 'sources':sources})
b=save('queries.json', {'fixture_version':1, 'source_facts_only_oracle':True, 'queries':items})
(HERE/'FREEZE.sha256').write_text(f'{a}  sources.json\n{b}  queries.json\n')
print(f'sources={len(sources)}; query_pairs={len(queries)}; queries={len(items)}')
print((HERE/'FREEZE.sha256').read_text())
