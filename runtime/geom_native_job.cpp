// Native geometry job (GEOMETRY_JOB.md "Step 2"), stage 1: the triangles a mesh piece keeps, computed straight from
// the job's gathered input buffer (set 1) instead of by the job's command handlers. Covers the two common shapes
// (commands 4 6 8 9 12 14 [15] 30 35 37 0, op9 attribute mask 1); anything else is declined.
//
// What the job does, from its code (LS addresses of the job binary):
//   op6 (n, 0)            vertex count
//   op9 (off, 1, 0)       positions: big-endian float3, packed, at set 1 + off
//   op12 (off, ntri, 0)   triangles: big-endian u16 a b c at set 1 + off
//   op14 (off)            0x68 bytes copied (0x7580): P0 = viewport words (x: flags in bytes 0-1, w = left << 16 | right,
//                         h = top << 16 | bottom), P1 P2 P3 = projection rows, P4 = the w row, P5 = half sizes,
//                         P6 word 0 = near value
//   op15 (off)            0x40 bytes: the world matrix W (rows); identity without op15
//   op35 -> 0xD400        Ci = Pi W (i = 1, 2, 4; per row: x W0, + y W1, + W2 z, + w W3), k = 0.0625 when byte 0 of
//                         P0 is positive (signed), else 0.5; X = (P5.y + k) C4 + P5.x C1, Y = (P5.w + k) C4 + P5.z C2;
//                         the transform (0xD628, spu_fast_vertex_transform) and the cull (0xD7F8,
//                         spu_fast_triangle_cull) with c34 = 2^-23, c45 = 2^-22, c55 = 0x28800000, c52 = P6.x,
//                         c71 / c69 = scissor width / height (float(low half) - float(high half) of P0.w / P0.h),
//                         c57 = sign bit when byte 1 of P0 is positive (signed), area test on (ctx+688 unset)
// All float operations as the SPU's (saturating multiply / fused multiply-add / add, spu_ops.h).
#include <cfloat>
#include <immintrin.h>
#include <cmath>
#include <cstring>
#include <atomic>
#include <vector>

#include "kernel.h"

namespace {
inline float sat(float x) { return x > FLT_MAX ? FLT_MAX : x < -FLT_MAX ? -FLT_MAX : x != x ? FLT_MAX : x; }
inline float fm(float a, float b) { return sat(a * b); }
inline float fma_(float a, float b, float c) { return sat(std::fmaf(a, b, c)); }
inline float fa(float a, float b) { return sat(a + b); }
inline float fs(float a, float b) { return sat(a - b); }
inline u32 be32(const u8* p) { u32 v; memcpy(&v, p, 4); return __builtin_bswap32(v); }
inline u16 be16(const u8* p) { return (u16)(p[0] << 8 | p[1]); }
inline float bef(const u8* p) { const u32 v = be32(p); float f; memcpy(&f, &v, 4); return f; }
inline float u2f(u32 v) { float f; memcpy(&f, &v, 4); return f; }
inline u32 f2u(float f) { u32 v; memcpy(&v, &f, 4); return v; }
struct V4 { float x, y, z, w; };
V4 ld4(const u8* p) { return {bef(p), bef(p + 4), bef(p + 8), bef(p + 12)}; }
// cflts with scale 1: saturating, NaN -> 0
s32 cflts(float x) { return x != x ? 0 : x >= 2147483648.0f ? INT32_MAX : x <= -2147483648.0f ? INT32_MIN : (s32)x; }
}  // namespace

// set1: the job's gathered input (LS bytes); the command stream starts at its first halfword + 1. Fills `kept` with the
// kept triangles (u16 a b c each, host order). Returns false for a shape it does not cover.
static bool decline(const char* why, u32 a = 0, u32 b = 0) {
    static std::atomic<int> n{0};
    if (n++ < 8) LOG_I("geom nj: declined (%s %x %x)", why, a, b);
    return false;
}
extern bool g_geom_op_prof;
void geom_op_prof(u32 handler, u64 tsc);
bool geom_nj_kept(const u8* set1, u32 set1_size, std::vector<u16>& kept, u32& pad, u16 pad_abc[3]) {
    const u64 pt0 = g_geom_op_prof ? __rdtsc() : 0;
    static const u8 lens[41] = {1, 0, 0, 0, 2, 2, 3, 3, 3, 4, 2, 3, 4, 7, 2, 2, 2, 3, 2, 2, 3, 2, 3, 10, 3, 4, 3, 2, 3, 1, 1, 1, 1, 1, 1, 1, 1, 1, 2, 1, 1};
    u32 nv = 0, pos = 0, tri = 0, ntri = 0, p14 = 0, p15 = 0;
    u64 seen = 0;
    bool ok9 = false;
    for (u32 i = 1, guard = 0; i < 0x40 && guard < 16; guard++) {
        const u32 op = set1[2 * i];
        if (op >= 41 || !lens[op]) return decline("opcode", op, i);
        auto arg = [&](u32 k) { return (u32)be16(set1 + 2 * (i + k)); };
        switch (op) {
            case 0: case 4: case 8: case 30: case 35: case 37: break;
            case 6: nv = arg(1); break;
            case 9: pos = arg(1); ok9 = arg(2) == 1; break;
            case 12: tri = arg(1); ntri = arg(2); break;
            case 14: p14 = arg(1); break;
            case 15: p15 = arg(1); break;
            default: return decline("command", op, i);
        }
        seen |= 1ull << op;
        if (op == 0) break;
        i += lens[op];
    }
    const u64 need = 1ull << 0 | 1ull << 4 | 1ull << 6 | 1ull << 8 | 1ull << 9 | 1ull << 12 | 1ull << 14 | 1ull << 30 | 1ull << 35 | 1ull << 37;
    if ((seen & need) != need || !ok9 || !nv || !ntri) return decline("shape", (u32)seen, ok9 << 16 | (nv ? 1 : 0) << 8 | (ntri ? 1 : 0));
    if (pos + nv * 12 > set1_size || tri + ntri * 6 > set1_size || p14 + 0x68 > set1_size || (p15 && p15 + 0x40 > set1_size))
        return decline("sizes", nv, ntri);

    const u8* P = set1 + p14;
    const V4 P1 = ld4(P + 16), P2 = ld4(P + 32), P4 = ld4(P + 64), P5 = ld4(P + 80);
    V4 W[4] = {{1, 0, 0, 0}, {0, 1, 0, 0}, {0, 0, 1, 0}, {0, 0, 0, 1}};
    if (seen & 1ull << 15)
        for (int r = 0; r < 4; r++) W[r] = ld4(set1 + p15 + 16 * r);
    const auto row = [&](const V4& p) {   // x W0, + y W1, + W2 z, + w W3 (per component)
        const float* w0 = &W[0].x; const float* w1 = &W[1].x; const float* w2 = &W[2].x; const float* w3 = &W[3].x;
        float out[4];
        for (int c = 0; c < 4; c++) {
            float r = fm(p.x, w0[c]);
            r = fma_(p.y, w1[c], r);
            r = fma_(w2[c], p.z, r);
            out[c] = fma_(p.w, w3[c], r);
        }
        return V4{out[0], out[1], out[2], out[3]};
    };
    const V4 C1 = row(P1), C2 = row(P2), C4 = row(P4);
    const s8 b0 = (s8)P[0], b1 = (s8)P[1];
    const float k = b0 > 0 ? 0.0625f : 0.5f;
    const float sx = fa(P5.x, 0.0f), sxk = fa(P5.y, k), sy = fa(P5.z, 0.0f), syk = fa(P5.w, k);
    const auto comb = [&](float a, const V4& Ca, float b) {   // b C4 + a Ca
        return V4{fma_(b, C4.x, fm(a, Ca.x)), fma_(b, C4.y, fm(a, Ca.y)), fma_(b, C4.z, fm(a, Ca.z)), fma_(b, C4.w, fm(a, Ca.w))};
    };
    const V4 X = comb(sx, C1, sxk), Y = comb(sy, C2, syk);
    const float c34 = u2f(0x34000000), c16 = 1.0f;

    const u64 pt1 = pt0 ? __rdtsc() : 0;
    // transform (as spu_fast_vertex_transform), eight vertices a pass: host-order quads x y w 0 per vertex
    const u32 nv8 = (nv + 7) & ~7u;
    thread_local std::vector<__m128> vq_store;
    if (vq_store.size() < nv8) vq_store.resize(nv8 + 64);
    __m128* vq = vq_store.data();
    {
        const auto splat = [](float f) { return _mm256_set1_ps(f); };
        const __m256 c53 = splat(C4.x), c18 = splat(C4.y), c54 = splat(C4.z), c6 = splat(C4.w);
        const __m256 c28 = splat(X.x), c30 = splat(X.y), c31 = splat(X.z), c13 = splat(X.w);
        const __m256 c48 = splat(Y.x), c50 = splat(Y.y), c51 = splat(Y.z), c52v = splat(Y.w);
        const __m256 c34v = splat(c34), c16v = splat(c16), one = splat(1.0f);
        const __m256 hi = splat(FLT_MAX), lo = splat(-FLT_MAX);
        const auto st = [&](__m256 x) { return _mm256_max_ps(_mm256_min_ps(x, hi), lo); };
        for (u32 v = 0; v < nv8; v += 8) {
            alignas(32) float xs[8], ys[8], zs[8];
            for (u32 k = 0; k < 8; k++) {
                const u32 vi = v + k < nv ? v + k : 0;   // (lanes past the end: vertex 0, not used)
                const u8* q = set1 + pos + vi * 12;
                xs[k] = bef(q); ys[k] = bef(q + 4); zs[k] = bef(q + 8);
            }
            const __m256 x = _mm256_load_ps(xs), y = _mm256_load_ps(ys), z = _mm256_load_ps(zs);
            const __m256 pp = st(_mm256_fmadd_ps(c18, y, st(_mm256_fmadd_ps(x, c53, c6))));
            const __m256 qq = st(_mm256_fmadd_ps(c30, y, st(_mm256_fmadd_ps(x, c28, c13))));
            const __m256 uu = st(_mm256_fmadd_ps(c50, y, st(_mm256_fmadd_ps(x, c48, c52v))));
            const __m256 w = st(_mm256_fmadd_ps(z, c54, pp)), hy = st(_mm256_fmadd_ps(z, c51, uu)), hx = st(_mm256_fmadd_ps(z, c31, qq));
            const __m256 est = st(_mm256_div_ps(one, w));
            const __m256 inv = st(_mm256_fmadd_ps(st(_mm256_fnmadd_ps(est, w, c16v)), est, est));
            const __m256 ox = st(_mm256_fmsub_ps(inv, hx, c34v)), oy = st(_mm256_fmsub_ps(inv, hy, c34v));
            const __m256 zero = _mm256_setzero_ps();
            const __m256 t0 = _mm256_unpacklo_ps(ox, oy), t1 = _mm256_unpackhi_ps(ox, oy);
            const __m256 t2 = _mm256_unpacklo_ps(w, zero), t3 = _mm256_unpackhi_ps(w, zero);
            const __m256 q0 = _mm256_shuffle_ps(t0, t2, 0x44), q1 = _mm256_shuffle_ps(t0, t2, 0xEE);
            const __m256 q2 = _mm256_shuffle_ps(t1, t3, 0x44), q3 = _mm256_shuffle_ps(t1, t3, 0xEE);
            vq[v + 0] = _mm256_castps256_ps128(q0); vq[v + 1] = _mm256_castps256_ps128(q1);
            vq[v + 2] = _mm256_castps256_ps128(q2); vq[v + 3] = _mm256_castps256_ps128(q3);
            vq[v + 4] = _mm256_extractf128_ps(q0, 1); vq[v + 5] = _mm256_extractf128_ps(q1, 1);
            vq[v + 6] = _mm256_extractf128_ps(q2, 1); vq[v + 7] = _mm256_extractf128_ps(q3, 1);
        }
    }

    const u64 pt2 = pt0 ? __rdtsc() : 0;
    // cull (as spu_fast_triangle_cull), four triangles a pass, compacted in order
    const u32 pw = be32(P + 8), ph = be32(P + 12);
    const float c71f = fs((float)(pw & 0xFFFF), (float)(pw >> 16)), c69f = fs((float)(ph & 0xFFFF), (float)(ph >> 16));
    const __m128 k34 = _mm_set1_ps(u2f(0x34000000)), c55 = _mm_set1_ps(u2f(0x28800000)), c45 = _mm_set1_ps(u2f(0x34800000));
    const __m128 c52 = _mm_set1_ps(bef(P + 96)), c69 = _mm_set1_ps(c69f), c71 = _mm_set1_ps(c71f);
    const __m128 c57 = _mm_castsi128_ps(_mm_set1_epi32(b1 > 0 ? (int)0x80000000 : 0));
    const __m128 sign = _mm_castsi128_ps(_mm_set1_epi32(INT32_MIN));
    const auto sat4 = [](__m128 x) { return _mm_max_ps(_mm_min_ps(x, _mm_set1_ps(FLT_MAX)), _mm_set1_ps(-FLT_MAX)); };
    const auto cvt = [](__m128 x) {   // cflts, scale 1
        __m128i v = _mm_cvttps_epi32(x);
        if (__builtin_expect(_mm_movemask_epi8(_mm_cmpeq_epi32(v, _mm_set1_epi32(INT32_MIN))) != 0, 0)) {
            alignas(16) float f[4]; alignas(16) s32 r[4];
            _mm_store_ps(f, x);
            for (int k = 0; k < 4; k++) r[k] = cflts(f[k]);
            v = _mm_load_si128((const __m128i*)r);
        }
        return v;
    };
    kept.resize(ntri * 3 + 12);
    u16* out = kept.data();
    u32 nkept = 0, oob = 0, last_keep = 1;
    for (u32 t = 0; t < ntri; t += 4) {
        const u32 m = ntri - t < 4 ? ntri - t : 4;
        u32 ia[4], ib[4], ic[4];
        for (u32 k = 0; k < 4; k++) {
            const u8* q = set1 + tri + (t + (k < m ? k : 0)) * 6;
            ia[k] = be16(q); ib[k] = be16(q + 2); ic[k] = be16(q + 4);
            oob |= (u32)(ia[k] >= nv) | (u32)(ib[k] >= nv) | (u32)(ic[k] >= nv);
            ia[k] = ia[k] < nv ? ia[k] : 0; ib[k] = ib[k] < nv ? ib[k] : 0; ic[k] = ic[k] < nv ? ic[k] : 0;
        }
        __m128 ax = vq[ia[0]], ay = vq[ia[1]], az = vq[ia[2]], aw = vq[ia[3]];
        __m128 bx = vq[ib[0]], by = vq[ib[1]], bz = vq[ib[2]], bw = vq[ib[3]];
        __m128 cx = vq[ic[0]], cy = vq[ic[1]], cz = vq[ic[2]], cw = vq[ic[3]];
        _MM_TRANSPOSE4_PS(ax, ay, az, aw);
        _MM_TRANSPOSE4_PS(bx, by, bz, bw);
        _MM_TRANSPOSE4_PS(cx, cy, cz, cw);
        const __m128 dcy = sat4(_mm_sub_ps(cy, ay)), dbx = sat4(_mm_sub_ps(bx, ax)), dby = sat4(_mm_sub_ps(by, ay)), dcx = sat4(_mm_sub_ps(cx, ax));
        const __m128 gx = _mm_cmpgt_ps(ax, bx), gy = _mm_cmpgt_ps(ay, by);
        __m128 maxx = _mm_blendv_ps(bx, ax, gx), minx = _mm_blendv_ps(ax, bx, gx);
        __m128 maxy = _mm_blendv_ps(by, ay, gy), miny = _mm_blendv_ps(ay, by, gy);
        maxy = _mm_blendv_ps(maxy, cy, _mm_cmpgt_ps(cy, maxy));
        miny = _mm_blendv_ps(cy, miny, _mm_cmpgt_ps(cy, miny));
        maxx = _mm_blendv_ps(maxx, cx, _mm_cmpgt_ps(cx, maxx));
        minx = _mm_blendv_ps(cx, minx, _mm_cmpgt_ps(cx, minx));
        const __m128 sum_y = sat4(_mm_add_ps(_mm_andnot_ps(sign, dcy), _mm_andnot_ps(sign, dby)));
        const __m128 sum_x = sat4(_mm_add_ps(_mm_andnot_ps(sign, dbx), _mm_andnot_ps(sign, dcx)));
        const __m128 margin = sat4(_mm_fmadd_ps(k34, sat4(_mm_add_ps(sum_x, sum_y)), c55));
        const __m128 area = sat4(_mm_fmadd_ps(dcx, dby, sat4(_mm_fnmadd_ps(dcy, dbx, margin))));
        const __m128 facing = _mm_xor_ps(c57, area);
        const __m128 hx = sat4(_mm_add_ps(c45, maxx)), hy = sat4(_mm_add_ps(c45, maxy));
        const __m128 same_col = _mm_castsi128_ps(_mm_cmpeq_epi32(cvt(minx), cvt(hx)));
        const __m128 same_row = _mm_castsi128_ps(_mm_cmpeq_epi32(cvt(miny), cvt(hy)));
        const __m128 drop = _mm_or_ps(_mm_or_ps(_mm_or_ps(same_col, same_row), _mm_or_ps(hx, hy)),
                                      _mm_or_ps(facing, _mm_or_ps(_mm_cmpgt_ps(minx, c71), _mm_cmpgt_ps(miny, c69))));
        const __m128 nearv = _mm_or_ps(_mm_cmpgt_ps(c52, az), _mm_or_ps(_mm_cmpgt_ps(c52, bz), _mm_cmpgt_ps(c52, cz)));
        const u32 keep = (u32)(~_mm_movemask_ps(drop) | _mm_movemask_ps(nearv));
        for (u32 k = 0; k < m; k++)   // kept ones in order (a branch beat the SPU loop's store-every-time here)
            if ((keep >> k) & 1) {
                u16* o = out + 3 * nkept++;
                o[0] = (u16)ia[k]; o[1] = (u16)ib[k]; o[2] = (u16)ic[k];
            }
        last_keep = (keep >> (m - 1)) & 1;
    }
    if (oob) return false;   // a vertex the job did not transform (its LS holds stale data)

    if (pt0) { const u64 pt3 = __rdtsc(); geom_op_prof(16, pt1 - pt0); geom_op_prof(17, pt2 - pt1); geom_op_prof(18, pt3 - pt2); }
    // the slot after the kept ones holds the last triangle when it was dropped (the store before each decision):
    // padding the GPU does not read, kept identical
    pad = ~0u;
    if (!last_keep) {
        const u8* q = set1 + tri + (ntri - 1) * 6;
        pad = ntri - 1;
        pad_abc[0] = be16(q); pad_abc[1] = be16(q + 2); pad_abc[2] = be16(q + 4);
    }
    kept.resize(nkept * 3);
    return true;
}
