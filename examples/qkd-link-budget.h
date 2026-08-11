#ifndef QKD_TESTBED_LINK_BUDGET_H
#define QKD_TESTBED_LINK_BUDGET_H

#include <cmath>
#include <cstdint>
#include <limits>
#include <stdexcept>
#include <string>

namespace qkd_testbed
{

// Default parameters for the distance-aware QKD link-budget model:
// R(L) = R0 * 10^(-alpha * L / 10).
// The README records the source and assumptions behind their current values.
constexpr double DEFAULT_ZERO_LOSS_KEY_RATE_BPS = 17094000.0;
constexpr double DEFAULT_FIBER_LENGTH_KM = 85.0;
constexpr double DEFAULT_FIBER_ATTENUATION_DB_PER_KM = 0.2417;

struct QkdLinkBudget
{
    double fiberLengthKm;
    double attenuationDbPerKm;
    double totalLossDb;
    double zeroLossKeyRateBps;
    uint64_t keyRateBps;
};

inline QkdLinkBudget
CalculateQkdLinkBudget(double fiberLengthKm,
                       double attenuationDbPerKm,
                       double zeroLossKeyRateBps = DEFAULT_ZERO_LOSS_KEY_RATE_BPS)
{
    if (!std::isfinite(fiberLengthKm) || fiberLengthKm < 0.0)
    {
        throw std::invalid_argument("fiberLengthKm must be finite and non-negative");
    }
    if (!std::isfinite(attenuationDbPerKm) || attenuationDbPerKm < 0.0)
    {
        throw std::invalid_argument("fiberAttenuationDbPerKm must be finite and non-negative");
    }
    if (!std::isfinite(zeroLossKeyRateBps) || zeroLossKeyRateBps <= 0.0)
    {
        throw std::invalid_argument("zeroLossKeyRateBps must be finite and greater than zero");
    }

    const double totalLossDb = attenuationDbPerKm * fiberLengthKm;
    const double calculatedRate = zeroLossKeyRateBps * std::pow(10.0, -totalLossDb / 10.0);
    if (!std::isfinite(calculatedRate) || calculatedRate < 1.0 ||
        calculatedRate > static_cast<double>(std::numeric_limits<uint64_t>::max()))
    {
        throw std::invalid_argument("the QKD link budget produces an unsupported key rate");
    }

    return {fiberLengthKm,
            attenuationDbPerKm,
            totalLossDb,
            zeroLossKeyRateBps,
            static_cast<uint64_t>(std::llround(calculatedRate))};
}

inline std::string
DescribeQkdLinkBudget(const std::string& linkName, const QkdLinkBudget& budget)
{
    return "[QKD_LINK_BUDGET] link=" + linkName +
           " fiberLengthKm=" + std::to_string(budget.fiberLengthKm) +
           " attenuationDbPerKm=" + std::to_string(budget.attenuationDbPerKm) +
           " totalLossDb=" + std::to_string(budget.totalLossDb) +
           " zeroLossKeyRateBps=" + std::to_string(budget.zeroLossKeyRateBps) +
           " keyRateBps=" + std::to_string(budget.keyRateBps);
}

} // namespace qkd_testbed

#endif // QKD_TESTBED_LINK_BUDGET_H
