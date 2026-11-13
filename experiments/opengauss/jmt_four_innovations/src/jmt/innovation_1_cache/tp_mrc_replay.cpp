// Bounded offline accelerator for the EXISTING read-only candidate-FIFO model.
// Supports only normal ReadBuffer strategy; caller rejects rings/dirty writes.
// No timestamps/observed-hit labels are used to make replacement decisions.
// CPU cost O(events * SB candidates), memory O(sampled capacities + events).
#include <algorithm>
#include <cstdint>
#include <cmath>
#include <fstream>
#include <iostream>
#include <list>
#include <stdexcept>
#include <unordered_map>
#include <unordered_set>
#include <vector>

struct Event {
    uint64_t ns;
    uint32_t tid, kind;
    int32_t page, buffer, observed;
    uint32_t pad;
};
static_assert(sizeof(Event)==32,"compact event ABI");

struct Ordered {
    std::list<int> order;
    std::unordered_map<int,std::list<int>::iterator> positions;
    bool has(int p) const {return positions.count(p);}
    void erase(int p) {auto it=positions.find(p);if(it!=positions.end()){order.erase(it->second);positions.erase(it);}}
    void put(int p) {erase(p);order.push_back(p);positions[p]=--order.end();}
    int pop() {int p=order.front();erase(p);return p;}
    size_t size() const{return order.size();}
};
struct FileCache {
    int capacity;
    uint64_t sequence=0;
    Ordered active,inactive,shadow;
    std::unordered_map<int,uint64_t> shadow_at;
    std::unordered_map<int,bool> referenced;
    explicit FileCache(int n):capacity(n){}
    void balance() {
        int target=capacity?std::max(1,capacity/2):0;
        while(int(active.size())>target){int p=active.pop();inactive.put(p);referenced[p]=false;}
        while(int(active.size()+inactive.size())>capacity){
            int p;
            if(inactive.size()){
                p=inactive.pop();bool ref=referenced[p];referenced.erase(p);
                if(ref&&int(active.size())<target){active.put(p);continue;}
            }else p=active.pop();
            shadow.put(p);shadow_at[p]=sequence;
            while(int(shadow.size())>std::max(16,capacity*4)){int q=shadow.pop();shadow_at.erase(q);}
        }
    }
    bool access(int p){
        ++sequence;if(!capacity)return false;
        if(active.has(p)){active.put(p);return true;}
        if(inactive.has(p)){inactive.erase(p);referenced.erase(p);active.put(p);balance();return true;}
        bool refault=false;
        if(shadow.has(p)){refault=sequence-shadow_at[p]<=uint64_t(std::max(1,capacity));shadow.erase(p);shadow_at.erase(p);}
        if(refault)active.put(p);else{inactive.put(p);referenced[p]=true;}
        balance();return false;
    }
    void add(int p){if(p<0||!capacity||active.has(p)||inactive.has(p))return;inactive.put(p);referenced[p]=true;balance();}
};
struct Stats {uint64_t accesses=0,sb=0,os=0,disk=0,observed=0,anomalies=0;};
struct Pool {
    int mib,capacity,startup;
    std::vector<int> pages,refs,begin,end,cursor;
    std::unordered_map<int,int> slots,actual,last;
    std::unordered_map<uint64_t,int> pins,external;
    std::unordered_map<uint32_t,std::vector<int>> pending;
    std::unordered_set<uint32_t> session_tids;
    FileCache file;
    Stats stats[2];
    std::string failure;
    uint64_t external_unpins=0,victim_pins=0;
    Pool(int mb,int cap,int os,int boot,int lists):mib(mb),capacity(cap),startup(boot),
        pages(cap,-1),refs(cap,0),file(os){
        lists=std::max(1,std::min(lists,boot));
        int width=boot/lists;
        for(int i=0;i<lists;i++){
            int b=i*width,e=std::min(cap,i==lists-1?boot:(i+1)*width);
            if(b<e){begin.push_back(b);end.push_back(e);cursor.push_back(b);}
        }
    }
    static uint64_t key(uint32_t tid,int p){return(uint64_t(tid)<<32)|uint32_t(p);}
    void pin(uint32_t tid,int p,int slot){auto k=key(tid,p);if(!pins[k])++refs[slot];++pins[k];}
    int victim(uint32_t tid){
        size_t n=begin.size(),first=tid%n;
        for(size_t offset=0;offset<n;offset++){
            size_t k=(first+offset)%n;int pos=cursor[k];
            for(int j=begin[k];j<end[k];j++){
                int index=pos;pos=pos+1>=end[k]?begin[k]:pos+1;
                if(!refs[index]){cursor[k]=pos;return index;}
            }
            cursor[k]=pos;
        }
        throw std::runtime_error("no replaceable sampled buffer");
    }
    void apply(const Event&e,int phase){
        int p=e.page;
        if(e.kind==1){
            session_tids.insert(e.tid);last[e.tid]=p;pending[e.tid].push_back(p);
            auto it=slots.find(p);bool hit=it!=slots.end();bool os_hit=false;
            if(hit)pin(e.tid,p,it->second);
            else{
                int slot=victim(e.tid),old=pages[slot];
                if(old>=0){slots.erase(old);file.add(old);}
                pages[slot]=p;refs[slot]=0;slots[p]=slot;pin(e.tid,p,slot);
                os_hit=file.access(p);
            }
            if(phase>=0){auto&s=stats[phase];++s.accesses;s.observed+=e.observed;
                if(hit)++s.sb;else if(os_hit)++s.os;else ++s.disk;}
        }else if(e.kind==8){
            auto it=last.find(e.tid);
            if(it!=last.end()){
                actual[e.buffer]=it->second;
                if(!slots.count(it->second)&&!external.count(key(e.tid,it->second)))
                    external[key(e.tid,it->second)]=1;
                auto&q=pending[e.tid];auto x=std::find(q.begin(),q.end(),it->second);
                if(x!=q.end())q.erase(x);
            }
        }else if(e.kind==2||e.kind==3){
            actual[e.buffer]=p;auto&q=pending[e.tid];auto it=std::find(q.begin(),q.end(),p);
            if(it!=q.end()){q.erase(it);return;}
            if(e.kind==3&&session_tids.count(e.tid)){++victim_pins;return;}
            auto slot=slots.find(p);
            if(slot!=slots.end())pin(e.tid,p,slot->second);else ++external[key(e.tid,p)];
        }else if(e.kind==5){
            auto k=key(e.tid,p);int priv=0;
            auto it=pins.find(k);if(it!=pins.end()){priv=it->second;pins.erase(it);}
            auto slot=slots.find(p);
            if(priv<=0||slot==slots.end()){
                auto ext=external.find(k);
                if(ext!=external.end()&&ext->second>0){if(!--ext->second)external.erase(ext);}
                else {++external_unpins;actual[e.buffer]=p;}
            }else refs[slot->second]=std::max(0,refs[slot->second]-1);
        }else if(e.kind==6)throw std::runtime_error("dirty workload outside read-only accelerator domain");
        else if(e.kind!=7)throw std::runtime_error("unsupported compact event");
    }
};
int main(int argc,char**argv){
    try{
        if(argc<10)throw std::runtime_error("events ppm startup pages OS pages lists t1 t2 t3 t4 SBs...");
        std::ifstream input(argv[1],std::ios::binary);
        if(!input)throw std::runtime_error("cannot open compact events");
        int ppm=std::stoi(argv[2]),boot=std::stoi(argv[3]),os=std::stoi(argv[4]),lists=std::stoi(argv[5]);
        uint64_t a=std::stoull(argv[6]),b=std::stoull(argv[7]),c=std::stoull(argv[8]),d=std::stoull(argv[9]);
        auto scale=[&](int pages){return std::max(1,int(std::nearbyint(double(pages)*ppm/1000000.)));};
        std::vector<Pool> pools;
        for(int i=10;i<argc;i++){int mib=std::stoi(argv[i]);pools.emplace_back(mib,scale(mib*128),scale(os),scale(boot),lists);}
        if(pools.empty())throw std::runtime_error("no SB candidate");
        Event e;std::unordered_set<uint32_t> tids;
        while(input.read(reinterpret_cast<char*>(&e),sizeof(e)))if(e.kind==1)tids.insert(e.tid);
        input.clear();input.seekg(0);
        for(auto&pool:pools)pool.session_tids=tids;
        uint64_t last=0,n=0;
        while(input.read(reinterpret_cast<char*>(&e),sizeof(e))){
            if(e.ns<last)throw std::runtime_error("compact events not timestamp ordered");
            last=e.ns;++n;int phase=e.ns>=a&&e.ns<b?0:(e.ns>=c&&e.ns<d?1:-1);
            if(e.ns>=d&&e.kind!=8)continue;
            for(auto&pool:pools)if(pool.failure.empty()){
                try{pool.apply(e,phase);}
                catch(const std::exception&error){pool.failure=error.what();}
            }
        }
        if(!input.eof())throw std::runtime_error("partial compact record");
        std::cout<<"{\"events\":"<<n<<",\"rows\":[";
        for(size_t i=0;i<pools.size();i++){auto&p=pools[i];if(i)std::cout<<",";
            std::cout<<"{\"shared_buffers_mib\":"<<p.mib<<",\"miniature_pages\":"<<p.capacity
              <<",\"replay_error\":"<<(p.failure.empty()?"null":"\"no_replaceable_sampled_buffer\"")
              <<",\"external_unpins\":"<<p.external_unpins<<",\"victim_pins\":"<<p.victim_pins<<",\"windows\":[";
            for(int j=0;j<2;j++){if(j)std::cout<<",";auto&s=p.stats[j];
                std::cout<<"{\"accesses\":"<<s.accesses<<",\"sb_hits\":"<<s.sb<<",\"os_hits\":"<<s.os
                  <<",\"disk\":"<<s.disk<<",\"observed_sb_hits\":"<<s.observed<<",\"anomalies\":"<<s.anomalies<<"}";}
            std::cout<<"]";
            std::cout<<"}";
        }
        std::cout<<"]}\n";
    }catch(const std::exception&e){std::cerr<<e.what()<<"\n";return 1;}
}
