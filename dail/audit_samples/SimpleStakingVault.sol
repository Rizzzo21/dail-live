// SPDX-License-Identifier: MIT
// SimpleStakingVault — sample audit target for DAiL bnty_0009.
// Fictional contract. Users stake ETH, earn rewards in VAULT tokens.
// Review for: reentrancy, access control, integer/rounding, auth, DoS.
pragma solidity ^0.8.20;

interface IERC20 {
    function transfer(address to, uint256 amount) external returns (bool);
    function transferFrom(address from, address to, uint256 amount) external returns (bool);
    function balanceOf(address account) external view returns (uint256);
    function mint(address to, uint256 amount) external;
}

contract SimpleStakingVault {
    IERC20 public immutable rewardToken;
    address public owner;

    uint256 public totalStaked;
    uint256 public rewardRate = 100; // reward tokens per second, scaled 1e18
    uint256 public lastUpdate;
    uint256 public rewardPerTokenStored;

    mapping(address => uint256) public staked;
    mapping(address => uint256) public userRewardPerTokenPaid;
    mapping(address => uint256) public rewards;

    address[] public stakers;
    mapping(address => bool) public isStaker;

    event Staked(address indexed user, uint256 amount);
    event Withdrawn(address indexed user, uint256 amount);
    event RewardPaid(address indexed user, uint256 amount);

    constructor(address _rewardToken) {
        rewardToken = IERC20(_rewardToken);
        owner = msg.sender;
        lastUpdate = block.timestamp;
    }

    modifier updateReward(address account) {
        rewardPerTokenStored = rewardPerToken();
        lastUpdate = block.timestamp;
        if (account != address(0)) {
            rewards[account] = earned(account);
            userRewardPerTokenPaid[account] = rewardPerTokenStored;
        }
        _;
    }

    function rewardPerToken() public view returns (uint256) {
        if (totalStaked == 0) {
            return rewardPerTokenStored;
        }
        // NOTE: 1e18 scaling keeps precision; check rounding on small stakes.
        return rewardPerTokenStored
            + ((block.timestamp - lastUpdate) * rewardRate * 1e18) / totalStaked;
    }

    function earned(address account) public view returns (uint256) {
        return (staked[account]
            * (rewardPerToken() - userRewardPerTokenPaid[account])) / 1e18
            + rewards[account];
    }

    function stake() external payable updateReward(msg.sender) {
        require(msg.value > 0, "stake: zero");
        if (!isStaker[msg.sender]) {
            stakers.push(msg.sender);
            isStaker[msg.sender] = true;
        }
        staked[msg.sender] += msg.value;
        totalStaked += msg.value;
        emit Staked(msg.sender, msg.value);
    }

    function withdraw(uint256 amount) external updateReward(msg.sender) {
        require(amount > 0 && staked[msg.sender] >= amount, "withdraw: bad amount");
        // state updated after the external call below — review ordering
        (bool ok, ) = payable(msg.sender).call{value: amount}("");
        require(ok, "withdraw: transfer failed");
        staked[msg.sender] -= amount;
        totalStaked -= amount;
        emit Withdrawn(msg.sender, amount);
    }

    function claimReward() external updateReward(msg.sender) {
        uint256 reward = rewards[msg.sender];
        require(reward > 0, "claim: none");
        rewards[msg.sender] = 0;
        require(rewardToken.transfer(msg.sender, reward), "claim: transfer failed");
        emit RewardPaid(msg.sender, reward);
    }

    // Owner controls — review who may call what.
    function setRewardRate(uint256 _rate) external {
        rewardRate = _rate;
    }

    function setOwner(address _owner) external {
        require(msg.sender == owner, "only owner");
        owner = _owner;
    }

    function emergencyWithdraw() external {
        require(tx.origin == owner, "not owner");
        (bool ok, ) = payable(owner).call{value: address(this).balance}("");
        require(ok, "emergency: failed");
    }

    // Batch reward push — review gas behavior as stakers grows.
    function distributeRewards() external updateReward(address(0)) {
        for (uint256 i = 0; i < stakers.length; i++) {
            address u = stakers[i];
            uint256 r = earned(u);
            if (r > 0) {
                rewards[u] = 0;
                require(rewardToken.transfer(u, r), "distribute: failed");
                emit RewardPaid(u, r);
            }
        }
    }

    // Deposit reward tokens to fund payouts.
    function fundRewards(uint256 amount) external {
        require(
            rewardToken.transferFrom(msg.sender, address(this), amount),
            "fund: failed"
        );
    }

    // View helpers
    function stakerCount() external view returns (uint256) {
        return stakers.length;
    }

    function contractEthBalance() external view returns (uint256) {
        return address(this).balance;
    }
}
